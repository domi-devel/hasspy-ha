"""Pure core of the hasspy integration: model + debug lifecycle.

Deliberately free of Home Assistant imports so the state machine can be unit
tested (and reasoned about) in isolation. `store.py` and `debug.py` build the
HA-facing pieces on top of this.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .const import (
    CONTROL_DOMAINS,
    DEFAULT_BRIDGE_STALE_SECONDS,
    DEFAULT_GRACE_SECONDS,
    DEFAULT_LEASE_SECONDS,
    SCOPE_DEBUG,
    SCOPE_PRODUCTION,
)

# Grace is capped so a forgotten bridge cannot keep garbage forever; the cap is
# generous because the whole point is to survive a multi-hour crash.
MAX_GRACE_SECONDS = 48 * 3600.0


def build_unique_id(bridge: str, automation: str | None, key: str) -> str:
    """Stable unique_id for a dynamic entity.

    The separator is a character that is never slugified away by Home
    Assistant, so the ``key`` survives verbatim in the suggested entity_id.
    """
    return f"{bridge}|{automation or '_'}|{key}"


def default_object_id(bridge: str, automation: str | None, key: str) -> str:
    """Deterministic object_id so entity_ids never depend on a device name.

    A hasspy entity_id must be predictable and must survive a user renaming the
    bridge device, otherwise every dashboard that references it breaks.
    """
    parts = [bridge]
    if automation:
        parts.append(automation)
    parts.append(key)
    raw = "_".join(parts).lower()
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in raw)


@dataclass
class EntityRecord:
    """A dynamic entity hasspy asked us to create."""

    unique_id: str
    bridge: str
    key: str
    domain: str

    scope: str = SCOPE_PRODUCTION
    automation: str | None = None
    name: str | None = None
    object_id: str | None = None

    device_class: str | None = None
    unit: str | None = None
    state_class: str | None = None
    icon: str | None = None
    entity_category: str | None = None

    # Control descriptors (domain in CONTROL_DOMAINS). A control is a *setting*:
    # Home Assistant holds the value and the user edits it; hasspy reads it.
    min: float | None = None
    max: float | None = None
    step: float | None = None
    mode: str | None = None  # number: "box" | "slider"
    options: list[str] | None = None  # select
    has_date: bool | None = None  # datetime
    has_time: bool | None = None  # datetime

    attributes: dict[str, Any] = field(default_factory=dict)
    state: Any = None
    initial_state: Any = None

    # Lifetime
    persist: bool = True
    pinned: bool = False
    ttl: float = DEFAULT_LEASE_SECONDS
    last_seen: float = 0.0
    stale_since: float | None = None
    created_at: float = 0.0

    @property
    def is_control(self) -> bool:
        return self.domain in CONTROL_DOMAINS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EntityRecord:
        known = cls.__dataclass_fields__  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class BridgeRecord:
    """One hasspy process."""

    bridge: str
    name: str = ""
    scope: str = SCOPE_PRODUCTION
    run_id: str | None = None

    registered_at: float = 0.0
    last_seen: float = 0.0
    stale_after: float = DEFAULT_BRIDGE_STALE_SECONDS

    # When the bridge became *continuously* live. Set to `now` on (re)start and
    # whenever a heartbeat follows a gap longer than `stale_after`. Grace for
    # debug entities is counted from here, so a bridge returning from an outage
    # gets a fresh grace window instead of a mass deletion.
    live_since: float = 0.0

    # Retention policy for this bridge's debug entities.
    ttl: float = DEFAULT_LEASE_SECONDS
    grace: float = DEFAULT_GRACE_SECONDS

    # The app instances the bridge reports as loaded. Each entry is a plain dict
    # so hasspy can attach whatever it likes without a schema change here:
    #   {"id": "roller#0", "cls": "Roller", "loaded": True,
    #    "last_event": 1730000000.0, "errors": 0}
    automations: list[dict[str, Any]] = field(default_factory=list)

    # The most recent log lines the bridge sent (newest first). Kept for the
    # bridge's log entity so you can read the tail from Home Assistant.
    log: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BridgeRecord:
        known = cls.__dataclass_fields__  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


# --- lifecycle state machine ------------------------------------------------


def bridge_is_live(store, bridge_name: str, now: float) -> bool:
    """Whether a bridge has heartbeated within its `stale_after` window."""
    record = store.get_bridge(bridge_name)
    if record is None or record.last_seen <= 0:
        return False
    return (now - record.last_seen) <= record.stale_after


def note_bridge_seen(record: BridgeRecord, now: float) -> None:
    """Record a heartbeat, tracking when the bridge became continuously live."""
    if (
        record.live_since <= 0
        or record.last_seen <= 0
        or (now - record.last_seen) > record.stale_after
    ):
        record.live_since = now
    record.last_seen = now


def record_grace(store, record: EntityRecord) -> float:
    bridge = store.get_bridge(record.bridge)
    return bridge.grace if bridge else 0.0


def evaluate(store, record: EntityRecord, now: float) -> str:
    """Return ``keep`` | ``stale`` | ``collect`` for one debug record.

    See ``docs/DEBUG_ENTITIES.md``. The two invariants that matter:

    * While the owning bridge is unreachable, ageing is *frozen* - we never
      collect, so a crashed automation keeps its entities no matter how long it
      is down.
    * Grace is counted from the later of "went stale" and "the bridge has been
      continuously back", so a bridge returning from a long outage does not
      trigger a mass deletion.
    """
    bridge = store.get_bridge(record.bridge)

    if not bridge_is_live(store, record.bridge, now):
        return "stale" if record.stale_since is not None else "keep"

    if record.stale_since is None:
        return "stale" if now - record.last_seen > record.ttl else "keep"

    grace_ref = max(record.stale_since, bridge.live_since if bridge else 0.0)
    grace = min(record_grace(store, record), MAX_GRACE_SECONDS)
    return "collect" if now - grace_ref >= grace else "stale"


def should_revive(store, record: EntityRecord, now: float) -> bool:
    """Whether a stale record was re-asserted by a live owner since going stale."""
    if record.stale_since is None:
        return False
    if not bridge_is_live(store, record.bridge, now):
        return False
    bridge = store.get_bridge(record.bridge)
    return bool(bridge and record.last_seen >= record.stale_since)


def now_seconds() -> float:
    """Single clock source, UTC epoch seconds (never wall-clock)."""
    return time.time()
