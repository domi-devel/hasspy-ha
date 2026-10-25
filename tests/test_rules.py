"""Integration tests for one-shot rules (services, entities, firing).

These exercise the whole path the user cares about: arm a rule over a service
call, see it as an entity, have it fire once, and cancel it. The engine uses
Home Assistant's own trigger machinery, so a test can drive it by simply
changing a state.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from custom_components.hasspy.const import DOMAIN  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402

pytestmark = pytest.mark.usefixtures("setup_integration")

OVERRIDE = "select.speicher_override"
SOC = "sensor.battery_soc"


async def _call(hass: HomeAssistant, service: str, **data):
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )


async def _setup_controls(hass: HomeAssistant) -> None:
    """A select control (the target) and a percentage sensor (the trigger)."""
    await hass.services.async_call(
        DOMAIN, "register_bridge", {"bridge": "production"}, blocking=True
    )
    await _call(
        hass,
        "create_entity",
        bridge="production",
        automation="inverter",
        key="speicher_override",
        domain="select",
        name="Speicher override",
        object_id="speicher_override",
        options=["Kein Override", "Laden erzwingen", "Entladen erzwingen"],
        state="Laden erzwingen",
    )
    hass.states.async_set(SOC, "45", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()


# --- arm, display, fire -----------------------------------------------------


async def test_add_rule_returns_entities_and_arms(hass: HomeAssistant) -> None:
    await _setup_controls(hass)

    result = await _call(
        hass,
        "add_rule",
        name="Storage no override at 60% SoC",
        trigger={
            "platform": "numeric_state",
            "entity_id": SOC,
            "above": 60,
        },
        action={"entity_id": OVERRIDE, "value": "Kein Override"},
        bridge="production",
    )
    await hass.async_block_till_done()

    assert result["state"] == "armed"
    assert result["armed"] is True
    assert result["entity_id"].startswith("sensor.hasspy_rule_")

    state = hass.states.get(result["entity_id"])
    assert state.state == "armed"
    # HA's own trigger validation normalises `entity_id` to a list; the rule
    # stores the validated config, so the canonical form is what we expose.
    assert state.attributes["trigger"]["entity_id"] == [SOC]
    assert state.attributes["action"]["value"] == "Kein Override"
    assert state.attributes["trigger_description"] == "sensor.battery_soc above 60"
    assert state.attributes["cancel_entity_id"] == result["cancel_entity_id"]

    # The cancel button exists and is available while the rule is armed.
    button = hass.states.get(result["cancel_entity_id"])
    assert button is not None

    # Both entities live on their own device, not on the bridge device.
    from homeassistant.helpers import device_registry as dr

    entry_id = next(iter(hass.data[DOMAIN]))
    devices = dr.async_entries_for_config_entry(
        dr.async_get(hass), hass.data[DOMAIN][entry_id].entry.entry_id
    )
    assert any(d.name == "hasspy rules" for d in devices)
    assert any(d.name == "hasspy production" for d in devices)


async def test_rule_fires_once_and_retires(hass: HomeAssistant) -> None:
    """The core promise: it acts when the threshold is crossed, then stops."""
    await _setup_controls(hass)

    result = await _call(
        hass,
        "add_rule",
        name="no override at 60%",
        trigger={"platform": "numeric_state", "entity_id": SOC, "above": 60},
        action={"entity_id": OVERRIDE, "value": "Kein Override"},
    )
    await hass.async_block_till_done()
    assert hass.states.get(OVERRIDE).state == "Laden erzwingen"

    hass.states.async_set(SOC, "61", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()

    assert hass.states.get(OVERRIDE).state == "Kein Override"
    rule_state = hass.states.get(result["entity_id"])
    assert rule_state.state == "fired"
    assert rule_state.attributes["action_runs"] == 1
    assert rule_state.attributes["fired_at"] is not None

    # One-shot: a second crossing must not re-run the (now detached) action.
    hass.states.async_set(OVERRIDE, "Entladen erzwingen")
    hass.states.async_set(SOC, "70", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    assert hass.states.get(OVERRIDE).state == "Entladen erzwingen"
    assert hass.states.get(result["entity_id"]).attributes["action_runs"] == 1


async def test_cancel_button_disarms_a_rule(hass: HomeAssistant) -> None:
    await _setup_controls(hass)
    result = await _call(
        hass,
        "add_rule",
        name="no override at 60%",
        trigger={"platform": "numeric_state", "entity_id": SOC, "above": 60},
        action={"entity_id": OVERRIDE, "value": "Kein Override"},
    )
    await hass.async_block_till_done()

    await hass.services.async_call(
        "button", "press", {"entity_id": result["cancel_entity_id"]}, blocking=True
    )
    await hass.async_block_till_done()

    assert hass.states.get(result["entity_id"]).state == "cancelled"
    # A cancelled rule must not act even if the condition later comes true.
    hass.states.async_set(SOC, "80", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    assert hass.states.get(OVERRIDE).state == "Laden erzwingen"


# --- plausibility at the service boundary -----------------------------------


async def test_implausible_threshold_is_refused_with_a_clear_error(
    hass: HomeAssistant,
) -> None:
    await _setup_controls(hass)
    with pytest.raises(Exception, match="plausible range"):
        await _call(
            hass,
            "add_rule",
            name="impossible",
            trigger={"platform": "numeric_state", "entity_id": SOC, "above": 1500},
            action={"entity_id": OVERRIDE, "value": "Kein Override"},
        )


async def test_already_satisfied_rule_is_refused_until_asked(
    hass: HomeAssistant,
) -> None:
    await _setup_controls(hass)
    hass.states.async_set(SOC, "70", {"unit_of_measurement": "%"})

    with pytest.raises(Exception, match="already true"):
        await _call(
            hass,
            "add_rule",
            name="already there",
            trigger={"platform": "numeric_state", "entity_id": SOC, "above": 60},
            action={"entity_id": OVERRIDE, "value": "Kein Override"},
        )

    result = await _call(
        hass,
        "add_rule",
        name="already there",
        trigger={"platform": "numeric_state", "entity_id": SOC, "above": 60},
        action={"entity_id": OVERRIDE, "value": "Kein Override"},
        fire_now_if_true=True,
    )
    await hass.async_block_till_done()
    assert result["state"] == "fired"
    assert hass.states.get(OVERRIDE).state == "Kein Override"


async def test_bad_action_is_refused(hass: HomeAssistant) -> None:
    await _setup_controls(hass)
    with pytest.raises(Exception, match="has no option"):
        await _call(
            hass,
            "add_rule",
            name="bad option",
            trigger={"platform": "numeric_state", "entity_id": SOC, "above": 60},
            action={"entity_id": OVERRIDE, "value": "Nonsense"},
        )


async def test_arbitrary_service_action_needs_opt_in(hass: HomeAssistant) -> None:
    await _setup_controls(hass)
    with pytest.raises(Exception, match="allow_any_action"):
        await _call(
            hass,
            "add_rule",
            name="notify",
            trigger={"platform": "numeric_state", "entity_id": SOC, "above": 60},
            action={
                "action": "select.select_option",
                "target": {"entity_id": OVERRIDE},
                "data": {"option": "Kein Override"},
            },
        )


# --- point in time ---------------------------------------------------------


async def test_time_rule_arms_and_expires(hass: HomeAssistant) -> None:
    """A trigger-based rule with a short expiry is disarmed when it lapses."""
    await _setup_controls(hass)
    result = await _call(
        hass,
        "add_rule",
        name="lapsing",
        trigger={"platform": "numeric_state", "entity_id": SOC, "above": 60},
        action={"entity_id": OVERRIDE, "value": "Kein Override"},
        expires_in=1,
    )
    await hass.async_block_till_done()

    assert result["kind"] == "trigger"
    assert hass.states.get(result["entity_id"]).state == "armed"

    import asyncio

    await asyncio.sleep(1.2)
    await hass.async_block_till_done()
    assert hass.states.get(result["entity_id"]).state == "expired"
    # An expired rule must not act, even when the condition later holds.
    hass.states.async_set(SOC, "90", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    assert hass.states.get(OVERRIDE).state == "Laden erzwingen"


async def test_scheduled_rule_outlives_its_own_fire_time(
    hass: HomeAssistant,
) -> None:
    """A rule scheduled past the default 24 h expiry must still fire.

    The expiry timer used to be armed at `now + 24 h` regardless of the fire
    time, so `in: "2d"` (or a voice request for "tomorrow evening") expired
    before its moment arrived and the action silently never ran.
    """
    await _setup_controls(hass)
    result = await _call(
        hass,
        "add_rule",
        name="two days out",
        **{"in": "2d"},
        action={"entity_id": OVERRIDE, "value": "Kein Override"},
    )
    await hass.async_block_till_done()

    entry_id = next(iter(hass.data[DOMAIN]))
    record = hass.data[DOMAIN][entry_id].store.get_rule(result["rule_id"])
    assert record is not None
    assert record.fire_at is not None
    assert record.expires_at is not None
    assert record.expires_at >= record.fire_at, "expiry would beat the fire time"


async def test_time_rule_fires_the_action(hass: HomeAssistant) -> None:
    await _setup_controls(hass)
    result = await _call(
        hass,
        "add_rule",
        name="imminent",
        **{"in": "0.05"},
        action={"entity_id": OVERRIDE, "value": "Kein Override"},
    )
    await hass.async_block_till_done()

    import asyncio

    await asyncio.sleep(0.3)
    await hass.async_block_till_done()

    assert hass.states.get(result["entity_id"]).state == "fired"
    assert hass.states.get(OVERRIDE).state == "Kein Override"


# --- listing, cancelling, retention ----------------------------------------


async def test_list_rules_reports_armed_and_finished(hass: HomeAssistant) -> None:
    await _setup_controls(hass)
    await _call(
        hass,
        "add_rule",
        name="a",
        trigger={"platform": "numeric_state", "entity_id": SOC, "above": 60},
        action={"entity_id": OVERRIDE, "value": "Kein Override"},
    )
    await hass.async_block_till_done()

    listing = await _call(hass, "list_rules", active_only=True)
    assert listing["count"] == 1
    assert listing["rules"][0]["name"] == "a"
    assert listing["rules"][0]["armed"] is True


async def test_cancel_rule_service_can_forget(hass: HomeAssistant) -> None:
    await _setup_controls(hass)
    result = await _call(
        hass,
        "add_rule",
        name="a",
        trigger={"platform": "numeric_state", "entity_id": SOC, "above": 60},
        action={"entity_id": OVERRIDE, "value": "Kein Override"},
    )
    await hass.async_block_till_done()

    forgotten = await _call(hass, "cancel_rule", rule_id=result["rule_id"], forget=True)
    await hass.async_block_till_done()

    assert forgotten["forgotten"] is True
    assert hass.states.get(result["entity_id"]) is None
    assert hass.states.get(result["cancel_entity_id"]) is None
    listing = await _call(hass, "list_rules")
    assert listing["count"] == 0


async def test_cancel_unknown_rule_errors(hass: HomeAssistant) -> None:
    with pytest.raises(Exception, match="Unknown hasspy rule"):
        await _call(hass, "cancel_rule", rule_id="nope")


# --- persistence -----------------------------------------------------------


async def test_armed_rule_survives_a_reload(hass: HomeAssistant) -> None:
    """A reload (HACS update) must not silently disarm rules."""
    await _setup_controls(hass)
    result = await _call(
        hass,
        "add_rule",
        name="a",
        trigger={"platform": "numeric_state", "entity_id": SOC, "above": 60},
        action={"entity_id": OVERRIDE, "value": "Kein Override"},
    )
    await hass.async_block_till_done()

    entry_id = next(iter(hass.data[DOMAIN]))
    assert await hass.config_entries.async_reload(entry_id)
    await hass.async_block_till_done()

    record = hass.data[DOMAIN][entry_id].store.get_rule(result["rule_id"])
    assert record is not None and record.state == "armed"

    # Re-armed means it still fires after the reload.
    hass.states.async_set(SOC, "65", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    assert hass.states.get(OVERRIDE).state == "Kein Override"


# --- regressions -----------------------------------------------------------


async def test_firing_a_rule_without_a_bridge_creates_no_phantom_bridge(
    hass: HomeAssistant,
) -> None:
    """A user/voice rule has no bridge; logging it must not invent one.

    `hasspy.log` self-heals an unknown bridge by creating it, so the engine used
    to register a bridge literally named "rules" - which then rendered as a
    permanently offline device in the UI.
    """
    await _setup_controls(hass)
    await _call(
        hass,
        "add_rule",
        name="bridge-less rule",
        trigger={"platform": "numeric_state", "entity_id": SOC, "above": 60},
        action={"entity_id": OVERRIDE, "value": "Kein Override"},
    )
    await hass.async_block_till_done()

    hass.states.async_set(SOC, "70", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()

    entry_id = next(iter(hass.data[DOMAIN]))
    bridges = hass.data[DOMAIN][entry_id].store.bridges
    assert set(bridges) == {"production"}


async def test_rapid_re_crossings_fire_the_action_only_once(
    hass: HomeAssistant,
) -> None:
    """Leaving and re-entering the range must not re-run a one-shot rule."""
    await _setup_controls(hass)
    result = await _call(
        hass,
        "add_rule",
        name="once only",
        trigger={"platform": "numeric_state", "entity_id": SOC, "above": 60},
        action={"entity_id": OVERRIDE, "value": "Kein Override"},
    )
    await hass.async_block_till_done()

    for value in ("70", "50", "80"):
        hass.states.async_set(SOC, value, {"unit_of_measurement": "%"})
    await hass.async_block_till_done()

    state = hass.states.get(result["entity_id"])
    assert state.state == "fired"
    assert state.attributes["action_runs"] == 1


async def test_sweep_keeps_a_just_cancelled_old_rule(
    hass: HomeAssistant,
) -> None:
    """Retention runs from when a rule finished, not from when it was created.

    A rule armed yesterday and cancelled now must stay visible for a full day
    after the cancellation; measuring from `created_at` made it vanish on the
    next sweep.
    """
    await _setup_controls(hass)
    result = await _call(
        hass,
        "add_rule",
        name="old",
        trigger={"platform": "numeric_state", "entity_id": SOC, "above": 60},
        action={"entity_id": OVERRIDE, "value": "Kein Override"},
    )
    await hass.async_block_till_done()

    entry_id = next(iter(hass.data[DOMAIN]))
    runtime = hass.data[DOMAIN][entry_id]
    record = runtime.store.get_rule(result["rule_id"])
    assert record is not None

    # Pretend it was created two days ago, then cancel it now.
    record.created_at -= 2 * 24 * 3600
    await _call(hass, "cancel_rule", rule_id=result["rule_id"])
    await hass.async_block_till_done()
    assert record.state == "cancelled"
    assert record.finished_at is not None

    assert await runtime.rule_engine.async_sweep() == 0
    assert runtime.store.get_rule(result["rule_id"]) is not None

    # Once the window has passed *since it finished*, it is collected.
    record.finished_at -= 25 * 3600
    assert await runtime.rule_engine.async_sweep() == 1
    assert runtime.store.get_rule(result["rule_id"]) is None


async def test_entity_form_threshold_is_accepted(hass: HomeAssistant) -> None:
    """HA accepts a helper's entity_id as `above`; we must not reject it.

    `NUMERIC_STATE_THRESHOLD_SCHEMA` allows a number or the entity_id of a
    helper read at fire time. Rejecting the entity form made the natural rule
    ("above the thermostat's target") impossible to create.
    """
    await _setup_controls(hass)
    hass.states.async_set("input_number.min_soc", "55")
    await hass.async_block_till_done()

    result = await _call(
        hass,
        "add_rule",
        name="above the helper",
        trigger={
            "platform": "numeric_state",
            "entity_id": SOC,
            "above": "input_number.min_soc",
        },
        action={"entity_id": OVERRIDE, "value": "Kein Override"},
    )
    await hass.async_block_till_done()

    assert result["state"] == "armed"
    assert any("read at fire time" in w for w in result["warnings"])
    assert hass.states.get(result["entity_id"]).state == "armed"


async def test_firing_does_not_leak_a_script_instance(
    hass: HomeAssistant,
) -> None:
    """Every firing used to leave a Script in hass.data[DATA_SCRIPTS] forever."""
    from homeassistant.helpers.script import DATA_SCRIPTS

    await _setup_controls(hass)
    assert not hass.data.get(DATA_SCRIPTS)

    await _call(
        hass,
        "add_rule",
        name="no leak",
        trigger={"platform": "numeric_state", "entity_id": SOC, "above": 60},
        action={"entity_id": OVERRIDE, "value": "Kein Override"},
    )
    await hass.async_block_till_done()

    hass.states.async_set(SOC, "70", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()

    assert not hass.data.get(DATA_SCRIPTS)


async def test_payload_reports_a_renamed_entity_id(hass: HomeAssistant) -> None:
    """A user rename must be reflected in the service response, not overridden."""
    from homeassistant.helpers import entity_registry as er

    await _setup_controls(hass)
    result = await _call(
        hass,
        "add_rule",
        name="rename me",
        trigger={"platform": "numeric_state", "entity_id": SOC, "above": 60},
        action={"entity_id": OVERRIDE, "value": "Kein Override"},
    )
    await hass.async_block_till_done()

    er.async_get(hass).async_update_entity(
        result["entity_id"], new_entity_id="sensor.storage_rule"
    )
    await hass.async_block_till_done()

    listing = await _call(hass, "list_rules")
    assert listing["rules"][0]["entity_id"] == "sensor.storage_rule"


async def test_failing_action_ends_in_error_and_does_not_rerun(
    hass: HomeAssistant,
) -> None:
    """A broken action must be reported, not retried, and not claimed as done."""
    await _setup_controls(hass)
    result = await _call(
        hass,
        "add_rule",
        name="will fail",
        trigger={"platform": "numeric_state", "entity_id": SOC, "above": 60},
        action={
            "action": "nonexistent_domain.does_not_exist",
            "target": {"entity_id": OVERRIDE},
        },
        allow_any_action=True,
    )
    await hass.async_block_till_done()

    hass.states.async_set(SOC, "70", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()

    state = hass.states.get(result["entity_id"])
    assert state.state == "error"
    assert state.attributes["last_error"]
    assert state.attributes["action_runs"] == 0

    # No retry: a second crossing of the same threshold changes nothing.
    hass.states.async_set(SOC, "50", {"unit_of_measurement": "%"})
    hass.states.async_set(SOC, "80", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    assert hass.states.get(result["entity_id"]).state == "error"
