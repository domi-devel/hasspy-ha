"""Integration tests for control entities (number/select/switch/datetime).

A control is a setting: Home Assistant holds the value and the user edits it,
while hasspy reads it (from the `control_value` attribute) and may write it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import HomeAssistant  # noqa: E402

from custom_components.hasspy.const import DOMAIN  # noqa: E402

pytestmark = pytest.mark.usefixtures("setup_integration")


async def _call(hass: HomeAssistant, service: str, **data):
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )


async def _mk_control(hass: HomeAssistant, **data):
    bridge = data.pop("_bridge", "prod")
    await hass.services.async_call(
        DOMAIN, "register_bridge", {"bridge": bridge}, blocking=True
    )
    await hass.async_block_till_done()
    return await _call(hass, "create_entity", bridge=bridge, **data)


# --- number -----------------------------------------------------------------


async def test_number_control_is_writable_and_readable_by_hasspy(
    hass: HomeAssistant,
) -> None:
    created = await _mk_control(
        hass,
        automation="roller#0",
        key="sunrise_offset",
        domain="number",
        name="Shutter sunrise offset",
        object_id="shutter_sunrise_offset",
        min=-300,
        max=300,
        step=1,
        unit="min",
        state=50,
    )
    await hass.async_block_till_done()
    assert created["entity_id"] == "number.shutter_sunrise_offset"

    state = hass.states.get("number.shutter_sunrise_offset")
    assert state.state == "50.0"
    assert state.attributes["min"] == -300
    assert state.attributes["max"] == 300
    assert state.attributes["step"] == 1
    assert state.attributes["unit_of_measurement"] == "min"
    # What hasspy reads back:
    assert state.attributes["control_value"] == 50

    # The user (or an automation) sets it through the native service.
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": created["entity_id"], "value": -19},
        blocking=True,
    )
    await hass.async_block_till_done()
    state = hass.states.get(created["entity_id"])
    assert state.state == "-19.0"
    assert state.attributes["control_value"] == -19.0


async def test_number_control_persists_across_reload(hass: HomeAssistant) -> None:
    entry = None
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN, "register_bridge", {"bridge": "prod"}, blocking=True
    )
    await _call(
        hass,
        "create_entity",
        bridge="prod",
        key="offset",
        domain="number",
        min=0,
        max=100,
        object_id="persist_offset",
        state=7,
    )
    await hass.async_block_till_done()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("number.persist_offset").state == "7.0"


# --- select -----------------------------------------------------------------


async def test_select_control_options_and_change(hass: HomeAssistant) -> None:
    created = await _mk_control(
        hass,
        key="ad_skip_mode",
        domain="select",
        object_id="ad_skip_mode",
        options=["mute", "info", "music", "mixed"],
        state="mixed",
    )
    await hass.async_block_till_done()
    state = hass.states.get(created["entity_id"])
    assert state.state == "mixed"
    assert state.attributes["options"] == ["mute", "info", "music", "mixed"]

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": created["entity_id"], "option": "mute"},
        blocking=True,
    )
    await hass.async_block_till_done()
    state = hass.states.get(created["entity_id"])
    assert state.state == "mute"
    assert state.attributes["control_value"] == "mute"


# --- switch -----------------------------------------------------------------


async def test_switch_control_toggle(hass: HomeAssistant) -> None:
    created = await _mk_control(
        hass,
        key="shading_enabled",
        domain="switch",
        object_id="shading_enabled",
        state=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get(created["entity_id"]).state == "on"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": created["entity_id"]}, blocking=True
    )
    await hass.async_block_till_done()
    state = hass.states.get(created["entity_id"])
    assert state.state == "off"
    assert state.attributes["control_value"] is False


# --- datetime ---------------------------------------------------------------


async def test_datetime_control(hass: HomeAssistant) -> None:
    created = await _mk_control(
        hass,
        key="next_heating",
        domain="datetime",
        object_id="next_heating",
        has_date=True,
        has_time=True,
        state="2026-10-07T05:00:00+00:00",
    )
    await hass.async_block_till_done()
    state = hass.states.get(created["entity_id"])
    assert state is not None
    assert state.state.startswith("2026-10-07")
    assert state.attributes["control_value"] == "2026-10-07T05:00:00+00:00"


# --- write path (native services) -------------------------------------------


async def test_control_is_writable_via_native_services(hass: HomeAssistant) -> None:
    """hasspy writes a control with the domain's native service, no extra API."""
    await _mk_control(
        hass,
        key="setpoint",
        domain="number",
        min=-5,
        max=5,
        step=0.1,
        object_id="hasspy_setpoint",
        state=3,
    )
    await hass.async_block_till_done()

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.hasspy_setpoint", "value": -1.5},
        blocking=True,
    )
    await hass.async_block_till_done()
    state = hass.states.get("number.hasspy_setpoint")
    assert state.state == "-1.5"
    assert state.attributes["control_value"] == -1.5


async def test_set_entity_state_rejects_a_control(hass: HomeAssistant) -> None:
    from homeassistant.exceptions import HomeAssistantError

    await _mk_control(
        hass, key="n", domain="number", min=0, max=10, object_id="num_c", state=1
    )
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "set_entity_state",
            {"bridge": "prod", "key": "n", "state": 5},
            blocking=True,
        )


# --- lifecycle --------------------------------------------------------------


async def test_control_is_never_garbage_collected(
    hass: HomeAssistant, setup_integration
) -> None:
    """A control is a setting; even on a debug bridge it must survive."""
    _, runtime = setup_integration
    await hass.services.async_call(
        DOMAIN, "register_bridge", {"bridge": "dbg", "scope": "debug"}, blocking=True
    )
    created = await _call(
        hass,
        "create_entity",
        bridge="dbg",
        key="c",
        domain="number",
        min=0,
        max=10,
        object_id="dbg_control",
        state=4,
    )
    await hass.async_block_till_done()

    # Force it long past any deadline and sweep.
    record = runtime.store.get_entity("dbg", None, "c")
    assert record.is_control
    record.last_seen = 0
    record.stale_since = 0
    counts = await runtime.collector.async_sweep()
    assert counts["collected"] == 0
    assert hass.states.get(created["entity_id"]).state == "4.0"

    # release_session releases debug *values* only.
    released = await _call(hass, "release_session", bridge="dbg")
    assert released["count"] == 0
