"""Plausibility rules for one-shot rules, tested without Home Assistant.

`rules.py` is the HA-free core, so these run everywhere (the `pure` fixture in
conftest loads it as a synthetic package). The cases encode the user-facing
promises: "1500 % SoC" is refused, "already above the threshold" is refused
unless asked otherwise, and a control write must be something the control can
actually accept.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def rules(pure):
    return pure.rules


def _soc(state="45", unit="%", **attrs):
    return {"state": state, "attributes": {"unit_of_measurement": unit, **attrs}}


def _select(option, options):
    return {"state": option, "attributes": {"options": list(options)}}


# --- trigger plausibility ---------------------------------------------------


def test_rejects_percentage_threshold_above_100(rules) -> None:
    """'when SoC reaches 1500 %' can never fire - it is not a judgement call."""
    states = {"sensor.soc": _soc()}
    with pytest.raises(rules.RuleError, match=r"outside the plausible range"):
        rules.check_trigger(
            {"platform": "numeric_state", "entity_id": "sensor.soc", "above": 1500},
            states,
        )


def test_accepts_threshold_inside_declared_bounds(rules) -> None:
    states = {"sensor.soc": _soc()}
    warnings = rules.check_trigger(
        {"platform": "numeric_state", "entity_id": "sensor.soc", "above": 60},
        states,
    )
    assert warnings == []


def test_rejects_missing_trigger_entity(rules) -> None:
    with pytest.raises(rules.RuleError, match="does not exist"):
        rules.check_trigger(
            {"platform": "numeric_state", "entity_id": "sensor.nope", "above": 1},
            {},
        )


def test_rejects_contradictory_thresholds(rules) -> None:
    states = {"sensor.soc": _soc()}
    with pytest.raises(rules.RuleError, match="above.*must stay below"):
        rules.check_trigger(
            {
                "platform": "numeric_state",
                "entity_id": "sensor.soc",
                "above": 80,
                "below": 60,
            },
            states,
        )


def test_rejects_numeric_trigger_without_thresholds(rules) -> None:
    states = {"sensor.soc": _soc()}
    with pytest.raises(rules.RuleError, match="needs 'above' and/or 'below'"):
        rules.check_trigger(
            {"platform": "numeric_state", "entity_id": "sensor.soc"}, states
        )


def test_warns_when_already_above_but_still_checkable(rules) -> None:
    """Not an error here - `trigger_already_satisfied` is what rejects it."""
    states = {"sensor.soc": _soc("70")}
    warnings = rules.check_trigger(
        {"platform": "numeric_state", "entity_id": "sensor.soc", "above": 60},
        states,
    )
    assert any("already at 70" in w for w in warnings)


def test_uses_number_control_declared_min_max(rules) -> None:
    states = {"number.offset": {"state": "0", "attributes": {"min": -300, "max": 300}}}
    with pytest.raises(rules.RuleError, match="outside the plausible range -300"):
        rules.check_trigger(
            {"platform": "numeric_state", "entity_id": "number.offset", "above": 1500},
            states,
        )


def test_state_trigger_rejects_value_the_select_cannot_take(rules) -> None:
    states = {"select.mode": _select("Kein Override", ["Kein Override", "Laden"])}
    with pytest.raises(rules.RuleError, match="can never become"):
        rules.check_trigger(
            {"platform": "state", "entity_id": "select.mode", "to": "Nonsense"},
            states,
        )


def test_time_trigger_requires_at(rules) -> None:
    with pytest.raises(rules.RuleError, match="needs 'at'"):
        rules.check_trigger({"platform": "time"}, {})


def test_trigger_without_platform_is_rejected(rules) -> None:
    with pytest.raises(rules.RuleError, match="needs a 'platform'"):
        rules.check_trigger({"entity_id": "sensor.soc"}, {})


def test_trigger_entity_ids_reads_lists_and_templates_safely(rules) -> None:
    trigger = {
        "platform": "numeric_state",
        "entity_id": ["sensor.a", "sensor.b"],
        "to": "{{ states('sensor.c') }}",
    }
    assert rules.trigger_entity_ids(trigger) == ["sensor.a", "sensor.b"]


# --- action plausibility ----------------------------------------------------


def test_control_write_rejects_unknown_select_option(rules) -> None:
    states = {"select.mode": _select("Kein Override", ["Kein Override", "Laden"])}
    with pytest.raises(rules.RuleError, match="has no option"):
        rules.check_action(
            {"entity_id": "select.mode", "value": "Entladen"}, states
        )


def test_control_write_rejects_out_of_range_number(rules) -> None:
    states = {"number.offset": {"state": "0", "attributes": {"min": -300, "max": 300}}}
    with pytest.raises(rules.RuleError, match="outside number.offset's range"):
        rules.check_action({"entity_id": "number.offset", "value": 500}, states)


def test_control_write_rejects_non_control_domain(rules) -> None:
    states = {"sensor.soc": _soc()}
    with pytest.raises(rules.RuleError, match="is not a control"):
        rules.check_action({"entity_id": "sensor.soc", "value": 1}, states)


def test_switch_action_needs_a_boolean(rules) -> None:
    states = {"switch.x": {"state": "off", "attributes": {}}}
    with pytest.raises(rules.RuleError, match="boolean"):
        rules.check_action({"entity_id": "switch.x", "value": "yes"}, states)


def test_arbitrary_service_call_needs_opt_in(rules) -> None:
    action = {"action": "light.turn_on", "target": {"entity_id": "light.x"}}
    with pytest.raises(rules.RuleError, match="allow_any_action"):
        rules.check_action(action, {})
    assert rules.check_action(action, {}, allow_any_action=True) == []


def test_missing_target_entity_only_warns(rules) -> None:
    warnings = rules.check_action(
        {"entity_id": "select.later", "value": "x"}, {}
    )
    assert any("does not exist yet" in w for w in warnings)


# --- action translation -----------------------------------------------------


@pytest.mark.parametrize(
    ("action", "service"),
    [
        ({"entity_id": "select.m", "value": "a"}, "select.select_option"),
        ({"entity_id": "number.n", "value": 5}, "number.set_value"),
        ({"entity_id": "switch.s", "value": True}, "switch.turn_on"),
        ({"entity_id": "switch.s", "value": False}, "switch.turn_off"),
        ({"entity_id": "datetime.d", "value": "2026-01-01T00:00:00"}, "datetime.set_value"),
    ],
)
def test_action_translates_to_a_service_call(rules, action, service) -> None:
    sequence = rules.action_script_sequence(action)
    assert len(sequence) == 1
    assert sequence[0]["action"] == service


# --- whole-rule assembly ----------------------------------------------------


def test_build_rule_sets_default_expiry(rules) -> None:
    record, fire_now = rules.build_rule(
        name="soc rule",
        action={"entity_id": "select.m", "value": "a"},
        trigger={"platform": "numeric_state", "entity_id": "sensor.soc", "above": 60},
        states={"sensor.soc": _soc("45"), "select.m": _select("a", ["a", "b"])},
        now=1000.0,
    )
    assert fire_now is False
    assert record.expires_at == 1000.0 + rules.DEFAULT_RULE_EXPIRY_SECONDS
    assert record.is_active
    assert record.script_sequence[0]["action"] == "select.select_option"


def test_build_rule_refuses_a_condition_that_is_already_true(rules) -> None:
    """The headline gotcha: HA triggers fire on a transition, not on attach."""
    with pytest.raises(rules.RuleError, match="already true"):
        rules.build_rule(
            name="soc rule",
            action={"entity_id": "select.m", "value": "a"},
            trigger={
                "platform": "numeric_state",
                "entity_id": "sensor.soc",
                "above": 60,
            },
            states={"sensor.soc": _soc("70"), "select.m": _select("a", ["a"])},
            now=1000.0,
        )


def test_build_rule_fire_now_if_true_reports_it(rules) -> None:
    record, fire_now = rules.build_rule(
        name="soc rule",
        action={"entity_id": "select.m", "value": "a"},
        trigger={"platform": "numeric_state", "entity_id": "sensor.soc", "above": 60},
        states={"sensor.soc": _soc("70"), "select.m": _select("a", ["a"])},
        fire_now_if_true=True,
        now=1000.0,
    )
    assert fire_now is True
    assert record.state == rules.RULE_STATE_ARMED


def test_build_rule_rejects_both_trigger_flavours(rules) -> None:
    with pytest.raises(rules.RuleError, match="exactly one of"):
        rules.build_rule(
            name="x",
            action={"entity_id": "select.m", "value": "a"},
            trigger={"platform": "time", "at": "12:00:00"},
            fire_at=2000.0,
            states={"select.m": _select("a", ["a"])},
            now=1000.0,
        )


def test_build_rule_rejects_a_time_in_the_past(rules) -> None:
    with pytest.raises(rules.RuleError, match="already in the past"):
        rules.build_rule(
            name="x",
            action={"entity_id": "select.m", "value": "a"},
            fire_at=500.0,
            states={"select.m": _select("a", ["a"])},
            now=1000.0,
        )


# --- lifecycle helpers ------------------------------------------------------


def test_mark_fired_and_is_finished(rules) -> None:
    record = rules.RuleRecord(rule_id="r1", name="x")
    rules.mark_fired(record, now=5.0)
    assert record.state == rules.RULE_STATE_FIRED
    assert record.fired_at == 5.0
    assert record.finished_at == 5.0
    assert rules.is_finished(record)
    # Firing is not the same as the action having run: a failing action must
    # not be reported as an executed one.
    assert record.action_runs == 0
    rules.mark_action_ran(record)
    assert record.action_runs == 1


def test_rearmable_only_for_future_point_in_time(rules) -> None:
    future = rules.RuleRecord(rule_id="r1", name="x", fire_at=2000.0)
    past = rules.RuleRecord(rule_id="r2", name="x", fire_at=500.0)
    triggered = rules.RuleRecord(
        rule_id="r3",
        name="x",
        trigger={"platform": "state", "entity_id": "sensor.a", "to": "on"},
    )
    assert rules.rearmable(future, now=1000.0) is True
    assert rules.rearmable(past, now=1000.0) is False
    assert rules.rearmable(triggered, now=1000.0) is True


def test_unknown_state_after_load_becomes_error(rules) -> None:
    record = rules.RuleRecord.from_dict(
        {"rule_id": "r1", "name": "x", "state": "bogus"}
    )
    assert record.state == rules.RULE_STATE_ERROR
    assert "bogus" in (record.last_error or "")


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("5m", 300.0),
        ("90s", 90.0),
        ("2h", 7200.0),
        ("1d", 86400.0),
        ("300", 300.0),
        (300, 300.0),
        ("", None),
        ("soon", None),
    ],
)
def test_parse_relative_offset(rules, spec, expected) -> None:
    assert rules.parse_relative_offset(spec) == expected


def test_descriptions_are_human_readable(rules) -> None:
    record = rules.RuleRecord(
        rule_id="r1",
        name="x",
        trigger={"platform": "numeric_state", "entity_id": "sensor.soc", "above": 60},
        action={"entity_id": "select.speicher_override", "value": "Kein Override"},
    )
    assert "sensor.soc above 60" in rules.describe_trigger(record)
    assert "Kein Override" in rules.describe_action(record)



def test_entity_form_threshold_is_given_not_missing(rules) -> None:
    """An entity-id threshold counts as supplied, even though it is not numeric."""
    states = {"sensor.soc": _soc("10")}
    warnings = rules.check_trigger(
        {
            "platform": "numeric_state",
            "entity_id": "sensor.soc",
            "above": "input_number.min_soc",
        },
        states,
    )
    assert any("read at fire time" in w for w in warnings)


def test_absent_thresholds_are_still_rejected(rules) -> None:
    states = {"sensor.soc": _soc("10")}
    with pytest.raises(rules.RuleError, match="needs 'above' and/or 'below'"):
        rules.check_trigger(
            {"platform": "numeric_state", "entity_id": "sensor.soc"}, states
        )


def test_expiry_never_precedes_a_scheduled_fire_time(rules) -> None:
    """`in: 2d` with the 24 h default must not expire before it fires."""
    fire_at = 1000.0 + 2 * 24 * 3600
    record, _ = rules.build_rule(
        name="two days out",
        action={"entity_id": "select.m", "value": "a"},
        fire_at=fire_at,
        states={"select.m": _select("a", ["a"])},
        now=1000.0,
    )
    assert record.expires_at is not None
    assert record.expires_at >= fire_at
