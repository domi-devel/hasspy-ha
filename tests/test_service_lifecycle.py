"""The hasspy services must outlive a config-entry reload.

These live in their own module on purpose: ``test_integration.py`` sets the
``setup_integration`` fixture for every test in the file, and that fixture
already loads the integration -- which masks this bug. A test that has to
observe the *absence* of the services must start from a clean Home Assistant.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import HomeAssistant

from custom_components.hasspy.const import DOMAIN


async def test_services_survive_a_reload(hass: HomeAssistant) -> None:
    """A reload must leave the hasspy services registered.

    The services are registered in ``async_setup`` (Home Assistant's rule:
    services must exist even with no loaded config entry). Unloading the entry
    used to remove them again, so a reload -- which unloads and sets up the
    entry without re-running ``async_setup`` -- left hasspy with no services at
    all. Every publish then failed with "Service hasspy.create_entity not
    found", silently freezing every hasspy-managed entity in Home Assistant.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, "create_entity")

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, "create_entity")
    assert hass.services.has_service(DOMAIN, "set_entity_state")


async def test_services_survive_an_unload_and_setup_cycle(
    hass: HomeAssistant,
) -> None:
    """The unload/setup cycle HA runs on a HACS update keeps the services.

    This is the path that actually broke in production: the integration files
    were replaced on disk, HA reloaded the entry, and the services vanished.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, "create_entity")
    assert hass.services.has_service(DOMAIN, "set_entity_state")
