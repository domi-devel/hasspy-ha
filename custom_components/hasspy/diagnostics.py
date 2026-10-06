"""Diagnostics support: dump the whole hasspy model for bug reports."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    runtime = hass.data[DOMAIN][entry.entry_id]
    return {
        "bridges": [b.to_dict() for b in runtime.store.bridges.values()],
        "entities": [e.to_dict() for e in runtime.store.entities.values()],
        "live_entities": sorted(runtime._entities),
    }
