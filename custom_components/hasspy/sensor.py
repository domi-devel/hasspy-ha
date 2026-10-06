"""`sensor` platform for dynamic hasspy entities."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .entity import HasspyDynamicEntity

if TYPE_CHECKING:
    from .store import EntityRecord

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Spawn every stored sensor, then accept new ones from services."""
    runtime = entry.runtime_data
    runtime.register_platform("sensor", async_add_entities)
    await runtime.async_spawn_stored("sensor")


class HasspySensor(HasspyDynamicEntity, SensorEntity):
    """A sensor whose state is whatever hasspy last pushed."""

    def _coerce_device_class(self, value: str) -> str | None:
        try:
            return SensorDeviceClass(value)
        except ValueError:
            return None

    def _apply_descriptors(self) -> None:
        super()._apply_descriptors()
        record = self.record
        self._attr_native_unit_of_measurement = record.unit
        if record.state_class:
            try:
                self._attr_state_class = SensorStateClass(record.state_class)
            except ValueError:
                self._attr_state_class = None

    @property
    def native_value(self) -> Any:
        return self._coerce(self.record.state)

    def _coerce(self, value: Any) -> Any:
        """Turn a JSON string into the type the device_class requires.

        hasspy talks JSON, so it can only ever send a string. A timestamp or
        date sensor would otherwise be rejected by Home Assistant, so parse
        ISO-8601 back into a `datetime`/`date` here.
        """
        if not isinstance(value, str):
            return value
        device_class = getattr(self, "device_class", None)
        if device_class == SensorDeviceClass.TIMESTAMP:
            return dt_util.parse_datetime(value) or value
        if device_class == SensorDeviceClass.DATE:
            parsed = dt_util.parse_datetime(value)
            return parsed.date() if parsed is not None else value
        return value

    def apply_record(self, record: EntityRecord) -> None:
        self.record = record
        self._apply_descriptors()
        self.async_write_ha_state()
