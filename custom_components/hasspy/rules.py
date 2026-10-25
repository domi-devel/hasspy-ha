"""Pure core of the one-shot rule engine: model + plausibility checks.

Deliberately free of Home Assistant imports, like `lifecycle.py`, so the
validation rules can be unit tested against plain dicts.

A *rule* is a trigger plus an action, armed on demand:

  "set the storage override to 'Kein Override' when the SoC reaches 60 %"

Home Assistant semantics matter here and are the reason this module rejects
rules instead of silently accepting them: an HA trigger only fires on a
*transition*, so a rule whose condition is already satisfied can never fire.
That is the single most likely user error, and it is checked explicitly
(`rule_already_satisfied`).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .const import (
    CONTROL_DOMAINS,
    DEFAULT_RULE_EXPIRY_SECONDS,
    RULE_STATE_ARMED,
    RULE_STATE_CANCELLED,
    RULE_STATE_ERROR,
    RULE_STATE_EXPIRED,
    RULE_STATE_FIRED,
    RULE_STATES,
)

RuleStateSnapshot = Mapping[str, Mapping[str, Any]]

# Bounded ranges we can validate without knowing the device. A `unit` of "%" is
# the one case where a hard 0..100 bound is universally true, which is what
# makes "trigger above 1500 % SoC" rejectable at creation time.
PERCENT_UNIT = "%"
PERCENT_BOUNDS = (0.0, 100.0)


class RuleError(ValueError):
    """A rule request that must be rejected rather than armed."""


def now_seconds() -> float:
    """Single clock source, UTC epoch seconds (never wall-clock)."""
    return time.time()


def new_rule_id() -> str:
    """Short, collision-resistant id usable in an entity_id."""
    return f"r{int(time.time())}{int(time.time_ns() % 1_000_000):06d}"


@dataclass
class RuleRecord:
    """One armed (or finished) one-shot rule."""

    rule_id: str
    name: str

    bridge: str | None = None
    key: str | None = None

    # Exactly one of these describes when to fire:
    #  * `trigger`  - a normal Home Assistant trigger config (any platform),
    #  * `fire_at`  - an absolute instant (epoch seconds), used for
    #                 "in 5 minutes" / "at 12 o'clock".
    trigger: dict[str, Any] | None = None
    fire_at: float | None = None

    # The action to run. Either a control write
    #   {"entity_id": "select.x", "value": "y"}
    # or, only with `allow_any_action`, a full service-call mapping.
    action: dict[str, Any] = field(default_factory=dict)
    allow_any_action: bool = False
    script_sequence: list[dict[str, Any]] = field(default_factory=list)

    state: str = RULE_STATE_ARMED
    created_at: float = 0.0
    expires_at: float | None = None
    fired_at: float | None = None
    # When the rule reached a terminal state. Retention is measured from here,
    # not from created_at: cancelling a rule armed yesterday must not make it
    # disappear immediately.
    finished_at: float | None = None
    last_error: str | None = None
    cancel_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    action_runs: int = 0

    @property
    def is_active(self) -> bool:
        return self.state == RULE_STATE_ARMED

    @property
    def kind(self) -> str:
        return "point_in_time" if self.fire_at is not None else "trigger"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuleRecord:
        known = cls.__dataclass_fields__  # type: ignore[attr-defined]
        record = cls(**{k: v for k, v in data.items() if k in known})
        if record.state not in RULE_STATES:
            # Never leave an unknown state armed: a rule whose state we cannot
            # interpret must not fire. Keep the original value for diagnosis.
            original = record.state
            record.state = RULE_STATE_ERROR
            record.last_error = f"unknown rule state {original!r} after load"
        return record


# --- small helpers ----------------------------------------------------------


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _attributes(state: Mapping[str, Any] | None) -> dict[str, Any]:
    if not state:
        return {}
    attrs = state.get("attributes")
    return dict(attrs) if isinstance(attrs, Mapping) else {}


def _state_value(state: Mapping[str, Any] | None) -> Any:
    if not state:
        return None
    return state.get("state")


def entity_bounds(state: Mapping[str, Any] | None) -> tuple[float, float] | None:
    """The plausible numeric range for an entity, when it can be known.

    Uses the entity's declared `min`/`max` capability attributes first (number
    controls expose them), then the universal 0..100 for percentage units.
    """
    attrs = _attributes(state)
    low = _as_float(attrs.get("min"))
    high = _as_float(attrs.get("max"))
    if low is not None and high is not None and low < high:
        return (low, high)
    if attrs.get("unit_of_measurement") == PERCENT_UNIT:
        return PERCENT_BOUNDS
    return None


def _threshold(value: Any) -> float | None:
    """A numeric threshold from a trigger config.

    HA accepts either a literal number or the entity_id of a helper whose value
    is read at fire time (``NUMERIC_STATE_THRESHOLD_SCHEMA`` is
    ``Any(Coerce(float), All(str, entity_domain(...)))``). The entity form
    cannot be range-checked here, so it yields None - but it is still a *given*
    threshold, which ``threshold_is_given`` distinguishes from an absent one.
    """
    if isinstance(value, Mapping):
        # Tolerated spelling: {"above": {"entity_id": ...}}
        return None
    return _as_float(value)


def threshold_specs(trigger: Mapping[str, Any]) -> tuple[Any, Any]:
    """The raw (above, below) specs, which may be numbers, entity_ids or None."""
    above = trigger.get("above")
    below = trigger.get("below")
    if above is None:
        above = trigger.get("value_min")
    if below is None:
        below = trigger.get("value_max")
    return above, below


def threshold_is_given(value: Any) -> bool:
    """Whether a threshold was supplied in any form HA accepts."""
    if value is None:
        return False
    if isinstance(value, Mapping):
        return bool(value)
    if isinstance(value, str):
        return bool(value.strip())
    return True


def trigger_entity_ids(trigger: Mapping[str, Any] | None) -> list[str]:
    """Every entity_id a trigger config references."""
    if not trigger:
        return []
    found: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            if "." in value and not value.startswith(("{{", "{%")):
                found.append(value)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, Mapping):
            if "entity_id" in value:
                collect(value["entity_id"])

    for key in ("entity_id", "target"):
        collect(trigger.get(key))
    return found


def numeric_thresholds(trigger: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """The (above, below) thresholds of a numeric trigger, if they are numbers.

    Entity-form thresholds yield None here: HA resolves them at fire time, so
    they cannot be checked at creation.
    """
    above, below = threshold_specs(trigger)
    return _threshold(above), _threshold(below)


def describes_numeric(
    trigger: Mapping[str, Any] | None,
) -> bool:
    return bool(trigger) and trigger.get("platform") == "numeric_state"


# --- plausibility: trigger --------------------------------------------------


def check_trigger(
    trigger: Mapping[str, Any], states: RuleStateSnapshot
) -> list[str]:
    """Validate a trigger config against the live state. Raises `RuleError`.

    Returns *warnings* - conditions that are suspicious but not provably
    impossible (e.g. the source entity is currently unavailable).
    """
    if not isinstance(trigger, Mapping):
        raise RuleError("trigger must be an object")
    platform = trigger.get("platform")
    if not platform or not isinstance(platform, str):
        raise RuleError(
            "trigger needs a 'platform' (e.g. numeric_state, state, time, sun)"
        )

    entity_ids = trigger_entity_ids(trigger)
    for entity_id in entity_ids:
        if entity_id not in states:
            raise RuleError(f"trigger entity {entity_id!r} does not exist")

    warnings: list[str] = []

    if describes_numeric(trigger):
        if not entity_ids:
            raise RuleError(
                "numeric_state trigger needs an 'entity_id' to watch"
            )
        above, below = numeric_thresholds(trigger)
        raw_above, raw_below = threshold_specs(trigger)
        if not (threshold_is_given(raw_above) or threshold_is_given(raw_below)):
            raise RuleError(
                "numeric_state trigger needs 'above' and/or 'below' "
                "(a number, or the entity_id of a helper to read at fire time)"
            )
        # Entity-form thresholds cannot be range-checked: HA reads the helper's
        # value when the trigger evaluates.
        for raw in (raw_above, raw_below):
            if isinstance(raw, str) and _as_float(raw) is None:
                warnings.append(
                    f"threshold {raw!r} is read at fire time; it cannot be "
                    "range-checked now"
                )
        if above is not None and below is not None and above >= below:
            raise RuleError(
                f"'above' ({above:g}) must stay below 'below' ({below:g}), "
                "otherwise the trigger is contradictory"
            )

        for entity_id in entity_ids:
            state = states.get(entity_id)
            bounds = entity_bounds(state)
            if bounds is not None:
                low, high = bounds
                for label, value in (("above", above), ("below", below)):
                    if value is None:
                        continue
                    if not (low <= value <= high):
                        unit = _attributes(state).get("unit_of_measurement")
                        suffix = f" {unit}" if unit else ""
                        raise RuleError(
                            f"trigger {label}={value:g}{suffix} is outside the "
                            f"plausible range {low:g}..{high:g}{suffix} of "
                            f"{entity_id}"
                        )

            current = _as_float(_state_value(state))
            if current is None:
                warnings.append(
                    f"{entity_id} has no numeric state right now; the trigger "
                    "will be armed but cannot fire until it does"
                )
                continue
            if _numeric_already_true(current, above, below):
                warnings.append(
                    f"{entity_id} is already at {current:g}; the trigger only "
                    "fires on a change (see fire_now_if_true)"
                )

    if platform == "state":
        if not entity_ids:
            raise RuleError("state trigger needs an 'entity_id' to watch")
        for entity_id in entity_ids:
            state = states.get(entity_id)
            if state is None:
                continue
            options = _attributes(state).get("options")
            wanted = trigger.get("to")
            if isinstance(options, list) and isinstance(wanted, str):
                if wanted not in options:
                    raise RuleError(
                        f"{entity_id} can never become {wanted!r}; "
                        f"allowed values are {options}"
                    )

    if platform == "time" and trigger.get("at") is None:
        raise RuleError("time trigger needs 'at' (HH:MM[:SS] or an entity id)")

    return warnings


def _numeric_already_true(
    current: float, above: float | None, below: float | None
) -> bool:
    """Whether HA would consider the current value already inside the trigger.

    HA's numeric_state trigger fires on the *transition* into the range, so a
    value that is already inside it will never trigger a fresh rule.
    """
    if above is not None and not current > above:
        return False
    if below is not None and not current < below:
        return False
    return above is not None or below is not None


# --- plausibility: action ---------------------------------------------------


def check_action(
    action: Mapping[str, Any],
    states: RuleStateSnapshot,
    *,
    allow_any_action: bool = False,
) -> list[str]:
    """Validate the action. Raises `RuleError`; returns warnings."""
    if not isinstance(action, Mapping) or not action:
        raise RuleError("action must be a non-empty object")

    is_service_call = "action" in action or "service" in action
    if is_service_call:
        if not allow_any_action:
            raise RuleError(
                "calling an arbitrary service needs allow_any_action: true; "
                "pass {entity_id, value} to write a control instead"
            )
        service = action.get("action") or action.get("service")
        if not isinstance(service, str) or "." not in service:
            raise RuleError(
                "a service action needs 'action' or 'service' as domain.service"
            )
        return []

    entity_id = action.get("entity_id")
    if not isinstance(entity_id, str) or "." not in entity_id:
        raise RuleError("action needs an 'entity_id' (or a service call)")
    if "value" not in action:
        raise RuleError("a control action needs a 'value' to write")

    domain = entity_id.split(".", 1)[0]
    if domain not in CONTROL_DOMAINS:
        # A media player, light, cover or climate entity is not a *control*:
        # there is no single value to write, it takes a service call. The
        # escape hatch is `allow_any_action`, so point at it instead of only
        # listing the writable domains -- this message is read by the voice
        # assistant and echoed to the user, and without the hint the agent
        # concludes the request is impossible.
        raise RuleError(
            f"{entity_id} is not a control; writable domains are "
            f"{', '.join(CONTROL_DOMAINS)}. To run a service on it instead, "
            f"pass action={{'action': '{domain}.<service>', 'target': "
            f"{{'entity_id': '{entity_id}'}}}} together with "
            f"allow_any_action: true"
        )

    state = states.get(entity_id)
    value = action.get("value")
    warnings: list[str] = []

    if state is None:
        warnings.append(
            f"{entity_id} does not exist yet; the action will fail when it fires"
        )
        return warnings

    attrs = _attributes(state)

    if domain == "select":
        options = attrs.get("options")
        if isinstance(options, list) and value not in options:
            raise RuleError(
                f"{entity_id} has no option {value!r}; allowed are {options}"
            )
    elif domain == "number":
        numeric = _as_float(value)
        if numeric is None:
            raise RuleError(f"{entity_id} needs a numeric value, got {value!r}")
        low = _as_float(attrs.get("min"))
        high = _as_float(attrs.get("max"))
        if low is not None and high is not None and not (low <= numeric <= high):
            raise RuleError(
                f"{numeric:g} is outside {entity_id}'s range {low:g}..{high:g}"
            )
    elif domain == "switch":
        if not isinstance(value, bool):
            raise RuleError(
                f"{entity_id} takes a boolean value (true/false), got {value!r}"
            )
    elif domain == "datetime":
        if not isinstance(value, str) or not value.strip():
            raise RuleError(f"{entity_id} takes an ISO date/time string")

    if _state_value(state) in ("unavailable", "unknown"):
        warnings.append(f"{entity_id} is currently {_state_value(state)}")

    return warnings


def action_script_sequence(action: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Translate a rule action into an HA script sequence.

    Kept next to the validation so the two cannot drift: whatever passes
    `check_action` must be expressible here.
    """
    if "action" in action or "service" in action:
        return [dict(action)]
    entity_id = action["entity_id"]
    domain = entity_id.split(".", 1)[0]
    value = action.get("value")
    if domain == "number":
        return [
            {
                "action": "number.set_value",
                "target": {"entity_id": entity_id},
                "data": {"value": value},
            }
        ]
    if domain == "select":
        return [
            {
                "action": "select.select_option",
                "target": {"entity_id": entity_id},
                "data": {"option": value},
            }
        ]
    if domain == "switch":
        return [
            {
                "action": "switch.turn_on" if value else "switch.turn_off",
                "target": {"entity_id": entity_id},
            }
        ]
    if domain == "datetime":
        return [
            {
                "action": "datetime.set_value",
                "target": {"entity_id": entity_id},
                "data": {"datetime": value},
            }
        ]
    raise RuleError(f"cannot build a control write for {entity_id}")


# --- display helpers --------------------------------------------------------


def describe_trigger(record: RuleRecord) -> str:
    """A one-line human description, for dashboards and voice confirmations."""
    if record.fire_at is not None:
        return f"at {_format_epoch(record.fire_at)}"
    trigger = record.trigger or {}
    platform = trigger.get("platform", "?")
    entity_ids = trigger_entity_ids(trigger)
    target = ", ".join(entity_ids) if entity_ids else ""
    if platform == "numeric_state":
        above, below = numeric_thresholds(trigger)
        parts = []
        if above is not None:
            parts.append(f"above {above:g}")
        if below is not None:
            parts.append(f"below {below:g}")
        return f"{target} {' and '.join(parts)}".strip()
    if platform == "state":
        return f"{target} becomes {trigger.get('to')!r}"
    if platform == "time":
        return f"at {trigger.get('at')}"
    if platform == "sun":
        return f"sun {trigger.get('event')}"
    return f"{platform} {target}".strip()


def describe_action(record: RuleRecord) -> str:
    """A one-line description of what the rule does when it fires."""
    action = record.action
    if "action" in action or "service" in action:
        return str(action.get("action") or action.get("service"))
    return f"set {action.get('entity_id')} to {action.get('value')!r}"


def _format_epoch(value: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def parse_relative_offset(spec: str | None) -> float | None:
    """Seconds from a loose spec like ``"5m"``, ``"90s"``, ``"2h"`` or ``"300"``.

    Kept here (not in the service layer) so the accepted duration syntax is
    tested without Home Assistant.
    """
    if spec is None:
        return None
    text = str(spec).strip().lower()
    if not text:
        return None
    units = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    if text[-1] in units:
        try:
            return float(text[:-1]) * units[text[-1]]
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


# --- request assembly -------------------------------------------------------


def build_rule(
    *,
    name: str,
    action: Mapping[str, Any],
    trigger: Mapping[str, Any] | None = None,
    fire_at: float | None = None,
    states: RuleStateSnapshot,
    bridge: str | None = None,
    key: str | None = None,
    allow_any_action: bool = False,
    fire_now_if_true: bool = False,
    expires_in: float | None = DEFAULT_RULE_EXPIRY_SECONDS,
    now: float | None = None,
) -> tuple[RuleRecord, bool]:
    """Validate everything and build the record.

    Returns ``(record, fire_now)``. ``fire_now`` is True when the caller asked
    for `fire_now_if_true` and the trigger condition is already satisfied - the
    engine then runs the action immediately instead of refusing the rule.
    """
    if not isinstance(name, str) or not name.strip():
        raise RuleError("a rule needs a name")
    if (trigger is None) == (fire_at is None):
        raise RuleError(
            "give exactly one of 'trigger' (an HA trigger) or 'at'/'in' (a time)"
        )

    now = now_seconds() if now is None else now
    warnings: list[str] = []

    if fire_at is not None:
        if fire_at <= now:
            raise RuleError("the requested time is already in the past")

    already_true = False
    if trigger is not None:
        warnings.extend(check_trigger(trigger, states))
        already_true = trigger_already_satisfied(trigger, states)
        if already_true and not fire_now_if_true:
            raise RuleError(
                "the trigger condition is already true, and a Home Assistant "
                "trigger only fires on a change - this rule could never fire. "
                "Pass fire_now_if_true: true to run the action immediately."
            )

    warnings.extend(
        check_action(action, states, allow_any_action=allow_any_action)
    )

    record = RuleRecord(
        rule_id=new_rule_id(),
        name=name.strip(),
        bridge=bridge,
        key=key,
        trigger=dict(trigger) if trigger is not None else None,
        fire_at=fire_at,
        action=dict(action),
        allow_any_action=allow_any_action,
        script_sequence=action_script_sequence(action),
        created_at=now,
        warnings=warnings,
    )
    if expires_in is not None and expires_in > 0:
        record.expires_at = now + float(expires_in)
        # A point-in-time rule must outlive its own scheduled moment. Without
        # this, `in: "2d"` with the 24 h default expiry armed both timers and
        # the expiry won, so the action was silently never run.
        if fire_at is not None and record.expires_at < fire_at:
            record.expires_at = fire_at + float(expires_in)
    return record, already_true


def trigger_already_satisfied(
    trigger: Mapping[str, Any], states: RuleStateSnapshot
) -> bool:
    """Whether the trigger's target condition holds right now."""
    platform = trigger.get("platform")
    if platform == "numeric_state":
        above, below = numeric_thresholds(trigger)
        for entity_id in trigger_entity_ids(trigger):
            current = _as_float(_state_value(states.get(entity_id)))
            if current is None:
                continue
            if _numeric_already_true(current, above, below):
                return True
        return False
    if platform == "state":
        wanted = trigger.get("to")
        if wanted is None:
            return False
        for entity_id in trigger_entity_ids(trigger):
            if _state_value(states.get(entity_id)) == wanted:
                return True
        return False
    return False


def mark_fired(record: RuleRecord, *, now: float | None = None) -> None:
    """Record that the trigger fired.

    `action_runs` is incremented only once the action has actually run, so a
    failing action does not claim to have run.
    """
    now = now_seconds() if now is None else now
    record.state = RULE_STATE_FIRED
    record.fired_at = now
    record.finished_at = now


def mark_action_ran(record: RuleRecord) -> None:
    record.action_runs += 1


def mark_ended(
    record: RuleRecord, state: str, *, error: str | None = None
) -> None:
    if state not in RULE_STATES:
        raise ValueError(f"unknown rule state {state!r}")
    if record.state == state and record.finished_at is not None:
        return
    record.state = state
    record.finished_at = now_seconds()
    if error is not None:
        record.last_error = error


def is_finished(record: RuleRecord) -> bool:
    return record.state in (
        RULE_STATE_FIRED,
        RULE_STATE_CANCELLED,
        RULE_STATE_EXPIRED,
        RULE_STATE_ERROR,
    )


def rearmable(record: RuleRecord, now: float | None = None) -> bool:
    """Whether a stored rule should be armed again after a restart."""
    if record.state != RULE_STATE_ARMED:
        return False
    if record.fire_at is not None:
        now = now_seconds() if now is None else now
        return record.fire_at > now
    return True
