"""The hasspy companion integration.

This integration is the Home-Assistant-side half of `hasspy
<https://github.com/domi-devel/hasspy>`_: a registry-backed entity platform for
a standalone Python automation runtime, plus a lifecycle manager that keeps the
throw-away entities a *debugging* run creates from piling up while still letting
a crashed automation stay inspectable for hours.

It does not import hasspy; hasspy is a separate process that talks to Home
Assistant and calls this integration's services.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .runtime import HasspyRuntime
from .services import async_setup_services
from .store import HasspyStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["binary_sensor", "sensor"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services; they must exist even with no config entry."""
    hass.data.setdefault(DOMAIN, {})
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up hasspy from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    store = HasspyStore(hass)
    await store.async_load()

    runtime = HasspyRuntime(hass, entry, store)
    hass.data[DOMAIN][entry.entry_id] = runtime
    entry.runtime_data = runtime

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime.async_start()

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _LOGGER.info("hasspy integration ready")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the entry, keeping the persisted model for the next reload.

    This runs on every *reload* (a HACS update, an options change), so it must
    be purely in-memory: devices, entities and the store file all survive.
    """
    runtime: HasspyRuntime = hass.data[DOMAIN][entry.entry_id]
    runtime.async_stop()
    await runtime.store.async_flush()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        _async_remove_services(hass)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The entry was deleted for good: drop the store file."""
    await HasspyStore(hass).async_remove()


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.async_reload(entry.entry_id)


def _async_remove_services(hass: HomeAssistant) -> None:
    """Remove hasspy services when the (single) entry is gone."""
    if hass.data.get(DOMAIN):
        return
    for service in (
        "register_bridge",
        "remove_bridge",
        "heartbeat",
        "create_entity",
        "set_entity_state",
        "delete_entity",
        "list_bridges",
        "list_entities",
        "release_session",
        "sweep",
        "pin",
        "unpin",
    ):
        hass.services.async_remove(DOMAIN, service)
