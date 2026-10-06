"""Integration-level tests: services, entities and the debug lifecycle.

These run against a real Home Assistant test instance (provided by
`pytest-homeassistant-custom-component`). They cover the contract hasspy
depends on: service calls create/update/remove real, registry-backed entities.
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


# --- bridge registration ----------------------------------------------------


async def test_register_bridge_creates_device_and_presence_entities(
    hass: HomeAssistant, setup_integration
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "register_bridge",
        {"bridge": "production", "name": "hasspy prod", "scope": "production"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.production_bridge_online") is not None
    assert hass.states.get("binary_sensor.production_bridge_online").state == "on"
    assert hass.states.get("sensor.production_automations") is not None

    from homeassistant.helpers import device_registry as dr

    entry, _ = setup_integration
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    assert devices[0].name == "hasspy prod"
    assert (DOMAIN, "production") in devices[0].identifiers


async def test_heartbeat_makes_unknown_bridge_appear(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        DOMAIN, "heartbeat", {"bridge": "debug"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.debug_bridge_online").state == "on"


async def test_automations_sensor_reflects_heartbeat(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        DOMAIN,
        "register_bridge",
        {
            "bridge": "production",
            "automations": [
                {"id": "roller#0", "cls": "Roller", "loaded": True, "errors": 0}
            ],
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get("sensor.production_automations")
    assert state.state == "1"
    assert state.attributes["ids"] == ["roller#0"]


# --- dynamic entities -------------------------------------------------------


async def test_create_entity_returns_entity_id_and_exists(
    hass: HomeAssistant,
) -> None:
    await hass.services.async_call(
        DOMAIN, "register_bridge", {"bridge": "production"}, blocking=True
    )
    result = await _call(
        hass,
        "create_entity",
        bridge="production",
        automation="roller#0",
        key="up",
        domain="sensor",
        name="Roller up",
        device_class="timestamp",
        state="2026-10-06T05:00:00+00:00",
    )
    await hass.async_block_till_done()

    assert result["entity_id"] is not None
    assert result["scope"] == "production"
    state = hass.states.get(result["entity_id"])
    assert state is not None
    # HA composes friendly_name from the device + entity name.
    assert "Roller up" in state.attributes["friendly_name"]
    assert state.attributes["automation"] == "roller#0"
    assert state.attributes["hasspy_managed"] is True


async def test_set_entity_state_updates_existing_entity(
    hass: HomeAssistant,
) -> None:
    await hass.services.async_call(
        DOMAIN, "register_bridge", {"bridge": "production"}, blocking=True
    )
    created = await _call(
        hass,
        "create_entity",
        bridge="production",
        automation="pump",
        key="status",
        domain="sensor",
        name="Pump status",
        state="quiet",
    )
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        "set_entity_state",
        {
            "bridge": "production",
            "automation": "pump",
            "key": "status",
            "state": "running",
            "attributes": {"detail": "motor drawing power"},
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(created["entity_id"])
    assert state.state == "running"
    assert state.attributes["detail"] == "motor drawing power"


async def test_binary_sensor_coerces_free_form_state(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        DOMAIN, "register_bridge", {"bridge": "production"}, blocking=True
    )
    created = await _call(
        hass,
        "create_entity",
        bridge="production",
        automation="window",
        key="open",
        domain="binary_sensor",
        device_class="window",
        state="on",
    )
    await hass.async_block_till_done()
    assert hass.states.get(created["entity_id"]).state == "on"

    await hass.services.async_call(
        DOMAIN,
        "set_entity_state",
        {
            "bridge": "production",
            "automation": "window",
            "key": "open",
            "state": "off",
        },
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get(created["entity_id"]).state == "off"


async def test_create_entity_is_idempotent(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        DOMAIN, "register_bridge", {"bridge": "production"}, blocking=True
    )
    first = await _call(
        hass, "create_entity", bridge="production", key="k", domain="sensor", state=1
    )
    second = await _call(
        hass, "create_entity", bridge="production", key="k", domain="sensor", state=2
    )
    await hass.async_block_till_done()
    assert first["unique_id"] == second["unique_id"]
    assert first["entity_id"] == second["entity_id"]
    assert hass.states.get(first["entity_id"]).state == "2"


async def test_delete_entity_removes_it(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        DOMAIN, "register_bridge", {"bridge": "production"}, blocking=True
    )
    created = await _call(
        hass, "create_entity", bridge="production", key="k", domain="sensor", state=1
    )
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN, "delete_entity", {"bridge": "production", "key": "k"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(created["entity_id"]) is None


async def test_list_entities_filters_by_scope(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        DOMAIN, "register_bridge", {"bridge": "debug", "scope": "debug"}, blocking=True
    )
    await _call(
        hass,
        "create_entity",
        bridge="debug",
        key="prod_one",
        domain="sensor",
        scope="production",
    )
    await _call(
        hass, "create_entity", bridge="debug", key="dbg_one", domain="sensor",
        scope="debug",
    )
    await hass.async_block_till_done()

    debug_only = await _call(hass, "list_entities", scope="debug")
    assert {e["key"] for e in debug_only["entities"]} == {"dbg_one"}


async def test_unknown_entity_raises_clear_error(hass: HomeAssistant) -> None:
    from homeassistant.exceptions import HomeAssistantError

    await hass.services.async_call(
        DOMAIN, "register_bridge", {"bridge": "production"}, blocking=True
    )
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "set_entity_state",
            {"bridge": "production", "key": "nope", "state": 1},
            blocking=True,
        )


# --- debug lifecycle --------------------------------------------------------


async def test_release_session_removes_only_debug_entities(
    hass: HomeAssistant,
) -> None:
    await hass.services.async_call(
        DOMAIN, "register_bridge", {"bridge": "debug", "scope": "debug"}, blocking=True
    )
    prod = await _call(
        hass, "create_entity", bridge="debug", key="keep", domain="sensor",
        scope="production", state=1,
    )
    dbg = await _call(
        hass, "create_entity", bridge="debug", key="drop", domain="sensor",
        scope="debug", state=1,
    )
    await hass.async_block_till_done()

    released = await _call(hass, "release_session", bridge="debug")
    await hass.async_block_till_done()

    assert released["count"] == 1
    assert hass.states.get(dbg["entity_id"]) is None
    assert hass.states.get(prod["entity_id"]) is not None


async def test_sweep_dry_run_reports_verdicts(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        DOMAIN, "register_bridge", {"bridge": "debug", "scope": "debug"}, blocking=True
    )
    await _call(
        hass, "create_entity", bridge="debug", key="k", domain="sensor", scope="debug",
        state=1,
    )
    await hass.async_block_till_done()

    result = await _call(hass, "sweep", dry_run=True)
    assert result["dry_run"] is True
    assert list(result["verdicts"].values()) == ["keep"]


async def test_debug_entity_goes_unavailable_when_stale(
    hass: HomeAssistant, setup_integration
) -> None:
    """A stale debug entity stays visible but renders `unavailable`."""
    _, runtime = setup_integration
    await hass.services.async_call(
        DOMAIN, "register_bridge", {"bridge": "debug", "scope": "debug"}, blocking=True
    )
    created = await _call(
        hass, "create_entity", bridge="debug", key="k", domain="sensor", scope="debug",
        state=42,
    )
    await hass.async_block_till_done()
    assert hass.states.get(created["entity_id"]).state == "42"

    # Force the transition the collector would apply once the lease lapses.
    record = runtime.store.get_entity("debug", None, "k")
    record.stale_since = record.last_seen - 1
    entity = runtime.get_entity(record.unique_id)
    entity.async_write_ha_state()
    await hass.async_block_till_done()

    # The entity is still present (visible as a registry entry) but unavailable.
    assert record.stale_since is not None
    assert hass.states.get(created["entity_id"]).state == "unavailable"


async def test_pin_exempts_debug_entity_from_collection(
    hass: HomeAssistant, setup_integration
) -> None:
    _, runtime = setup_integration
    await hass.services.async_call(
        DOMAIN, "register_bridge", {"bridge": "debug", "scope": "debug"}, blocking=True
    )
    created = await _call(
        hass, "create_entity", bridge="debug", key="k", domain="sensor", scope="debug",
        state=1,
    )
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN, "pin", {"bridge": "debug"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(created["entity_id"]).state == "1"

    # A sweep with the entity long past any deadline still keeps it.
    record = runtime.store.get_entity("debug", None, "k")
    assert record.pinned is True
    record.last_seen = 0
    record.stale_since = 0
    counts = await runtime.collector.async_sweep()
    assert counts["collected"] == 0
    assert hass.states.get(created["entity_id"]) is not None


# --- persistence ------------------------------------------------------------


async def test_entities_survive_reload(hass: HomeAssistant) -> None:
    """A reload (e.g. HACS update) must not change entity_ids or lose entities."""
    entry = None
    from custom_components.hasspy.const import DOMAIN as D
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=D, data={}, unique_id=D)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        D, "register_bridge", {"bridge": "production"}, blocking=True
    )
    created = await _call(
        hass, "create_entity", bridge="production", key="k", domain="sensor",
        name="Keep me", state=7,
    )
    await hass.async_block_till_done()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(created["entity_id"])
    assert state is not None
    assert state.state == "7"
    assert "Keep me" in state.attributes["friendly_name"]
