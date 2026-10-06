"""`binary_sensor` platform for dynamic hasspy entities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import HasspyDynamicEntity, truthy

if TYPE_CHECKING:
    from .store import EntityRecord

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Spawn every stored binary sensor, then accept new ones from services."""
    runtime = entry.runtime_data
    runtime.register_platform("binary_sensor", async_add_entities)
    await runtime.async_spawn_stored("binary_sensor")


class HasspyBinarySensor(HasspyDynamicEntity, BinarySensorEntity):
    """A binary sensor fed by a free-form hasspy state."""

    def _coerce_device_class(self, value: str) -> str | None:
        try:
            return BinarySensorDeviceClass(value)
        except ValueError:
            return None

    @property
    def is_on(self) -> bool | None:
        return truthy(self.record.state)

    def apply_record(self, record: EntityRecord) -> None:
        self.record = record
        self._apply_descriptors()
        self.async_write_ha_state()
