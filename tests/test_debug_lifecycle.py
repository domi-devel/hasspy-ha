"""Unit tests for the debug-entity lifecycle (`lifecycle.evaluate`).

Runnable without Home Assistant: the pure core is loaded directly by the
`pure` fixture in `conftest.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

NOW = 1_000_000.0


@dataclass
class FakeStore:
    bridges: dict[str, Any] = field(default_factory=dict)

    def get_bridge(self, name: str):
        return self.bridges.get(name)


def make_bridge(pure, *, live=True, live_since=None, **overrides: Any):
    """A bridge record; `live=False` simulates a crashed/absent owner."""
    base = dict(
        bridge="debug",
        scope="debug",
        last_seen=NOW if live else NOW - 10 * 3600,
        stale_after=120.0,
        live_since=live_since if live_since is not None else NOW,
        ttl=60.0,
        grace=6 * 3600.0,
    )
    base.update(overrides)
    return pure.lifecycle.BridgeRecord(**base)


def make_entity(pure, **overrides: Any):
    base = dict(
        unique_id="debug|a|k",
        bridge="debug",
        key="k",
        domain="sensor",
        scope="debug",
        automation="a",
        ttl=60.0,
        last_seen=NOW - 10,  # fresh
    )
    base.update(overrides)
    return pure.lifecycle.EntityRecord(**base)


# --- basics -----------------------------------------------------------------


def test_fresh_entity_is_kept(pure):
    store = FakeStore({"debug": make_bridge(pure)})
    assert pure.lifecycle.evaluate(store, make_entity(pure), NOW) == "keep"


def test_lease_expiry_goes_stale_not_collected(pure):
    store = FakeStore({"debug": make_bridge(pure)})
    record = make_entity(pure, last_seen=NOW - 61)
    assert pure.lifecycle.evaluate(store, record, NOW) == "stale"


def test_stale_within_grace_is_still_stale(pure):
    store = FakeStore({"debug": make_bridge(pure)})
    record = make_entity(pure, last_seen=NOW - 120, stale_since=NOW - 60)
    assert pure.lifecycle.evaluate(store, record, NOW) == "stale"


def test_stale_past_grace_is_collected(pure):
    # Bridge has been continuously live since well before the entity went
    # stale, so the grace window is genuinely over.
    store = FakeStore(
        {"debug": make_bridge(pure, grace=100.0, live_since=NOW - 250)}
    )
    record = make_entity(pure, last_seen=NOW - 500, stale_since=NOW - 200)
    assert pure.lifecycle.evaluate(store, record, NOW) == "collect"


# --- the crash-window invariants -------------------------------------------


def test_aging_frozen_while_bridge_unreachable(pure):
    """A crashed owner must never lose its entities, however long it is gone."""
    store = FakeStore({"debug": make_bridge(pure, live=False)})
    fresh = make_entity(pure, last_seen=NOW - 10 * 3600)
    assert pure.lifecycle.evaluate(store, fresh, NOW) == "keep"

    stale = make_entity(pure, last_seen=NOW - 10 * 3600, stale_since=NOW - 10 * 3600)
    assert pure.lifecycle.evaluate(store, stale, NOW) == "stale"


def test_bridge_return_restarts_grace_window(pure):
    """A bridge down for 10h comes back to a fresh grace, not a mass deletion."""
    store = FakeStore({"debug": make_bridge(pure, live_since=NOW)})
    record = make_entity(pure, last_seen=NOW - 10 * 3600, stale_since=NOW - 10 * 3600)
    assert pure.lifecycle.evaluate(store, record, NOW) == "stale"


def test_bridge_live_longer_than_grace_collects(pure):
    store = FakeStore(
        {"debug": make_bridge(pure, grace=100.0, live_since=NOW - 200)}
    )
    record = make_entity(pure, last_seen=NOW - 10 * 3600, stale_since=NOW - 150)
    assert pure.lifecycle.evaluate(store, record, NOW) == "collect"


def test_dead_bridge_never_collects_even_past_grace(pure):
    """The strongest invariant: no live owner => no collection, ever."""
    store = FakeStore({"debug": make_bridge(pure, live=False, grace=1.0)})
    record = make_entity(pure, last_seen=NOW - 7200, stale_since=NOW - 7200)
    assert pure.lifecycle.evaluate(store, record, NOW) == "stale"


def test_missing_bridge_freezes(pure):
    store = FakeStore({})
    assert pure.lifecycle.evaluate(store, make_entity(pure), NOW) == "keep"


def test_revive_only_when_touched_since_stale(pure):
    store = FakeStore({"debug": make_bridge(pure)})
    touched = make_entity(pure, last_seen=NOW - 1, stale_since=NOW - 100)
    assert pure.lifecycle.should_revive(store, touched, NOW) is True

    untouched = make_entity(pure, last_seen=NOW - 200, stale_since=NOW - 100)
    assert pure.lifecycle.should_revive(store, untouched, NOW) is False


# --- identity ---------------------------------------------------------------


def test_unique_id_is_stable_and_bridge_scoped(pure):
    build = pure.lifecycle.build_unique_id
    assert build("debug", "roller#0", "up") == "debug|roller#0|up"
    assert build("prod", "roller#0", "up") == "prod|roller#0|up"
    assert build("debug", None, "up") == "debug|_|up"

