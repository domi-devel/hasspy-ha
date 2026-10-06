"""Writable control entities: number, select, switch, datetime.

A *control* is a setting, not a value:

* Home Assistant holds the value and the **user** edits it (via the UI).
* hasspy reads it when it needs the setting.
* hasspy may also write it (e.g. an optimizer nudging a setpoint) through the
  ``set_control`` service.

This is the inverse of the value platforms (sensor/binary_sensor), where hasspy
is the sole writer. Keeping that distinction explicit is what lets a control
live in the entity registry with a stable entity_id (``number.shutter_sunrise_offset``)
instead of a ``input_number`` helper.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .entity import HasspyDynamicEntity, truthy

PARALLEL_UPDATES = 0


async def async_setup_control_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    domain: str,
) -> None:
    """Shared setup for one control platform (number/select/switch/datetime).

    Home Assistant discovers platforms by module name, so the four thin
    platform modules (number.py, select.py, ...) each forward to this with
    their own domain.
    """
    runtime = entry.runtime_data
    runtime.register_platform(domain, async_add_entities)
    await runtime.async_spawn_stored(domain)


class HasspyControlMixin:
    """Shared write path for every control entity."""

    record: Any
    runtime: Any

    def _write_control(self, value: Any) -> None:
        """Persist a user/hasspy change and let hasspy see it."""
        self.record.state = value
        self.runtime.async_control_changed(self.record)
        self.async_write_ha_state()

    @property
    def control_value(self) -> Any:
        return self.record.state

    def apply_record(self, record: Any) -> None:
        self.record = record
        self._apply_descriptors()
        self.async_write_ha_state()


class HasspyNumber(HasspyControlMixin, HasspyDynamicEntity, NumberEntity):
    """A numeric setting (replaces an `input_number` helper)."""

    def _coerce_device_class(self, value: str) -> str | None:
        try:
            return NumberDeviceClass(value)
        except ValueError:
            return None

    def _apply_descriptors(self) -> None:
        super()._apply_descriptors()
        record = self.record
        self._attr_native_unit_of_measurement = record.unit
        self._attr_native_min_value = record.min
        self._attr_native_max_value = record.max
        self._attr_native_step = record.step if record.step is not None else 1.0
        if record.mode:
            try:
                self._attr_mode = NumberMode(record.mode)
            except ValueError:
                self._attr_mode = NumberMode.AUTO
        else:
            self._attr_mode = NumberMode.AUTO

    @property
    def native_value(self) -> float | None:
        try:
            return float(self.record.state)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        self._write_control(value)


class HasspySelect(HasspyControlMixin, HasspyDynamicEntity, SelectEntity):
    """A choice setting (replaces an `input_select` helper)."""

    def _apply_descriptors(self) -> None:
        super()._apply_descriptors()
        self._attr_options = list(self.record.options or [])

    @property
    def current_option(self) -> str | None:
        return self.record.state

    async def async_select_option(self, option: str) -> None:
        self._write_control(option)


class HasspySwitch(HasspyControlMixin, HasspyDynamicEntity, SwitchEntity):
    """A boolean setting (replaces an `input_boolean` helper)."""

    def _coerce_device_class(self, value: str) -> str | None:
        try:
            return SwitchDeviceClass(value)
        except ValueError:
            return None

    @property
    def is_on(self) -> bool | None:
        return truthy(self.record.state)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._write_control(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._write_control(False)


class HasspyDatetime(HasspyControlMixin, HasspyDynamicEntity, DateTimeEntity):
    """A date/time setting (replaces an `input_datetime` helper)."""

    def _apply_descriptors(self) -> None:
        super()._apply_descriptors()
        record = self.record
        # Default to a full datetime unless told otherwise. HA requires at
        # least one of the two to be set.
        has_date = record.has_date if record.has_date is not None else True
        has_time = record.has_time if record.has_time is not None else True
        if not has_date and not has_time:
            has_date = True
        self._attr_has_date = has_date
        self._attr_has_time = has_time

    @property
    def native_value(self) -> datetime | None:
        value = self.record.state
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day)
        return dt_util.parse_datetime(str(value))

    async def async_set_value(self, value: datetime) -> None:
        self._write_control(value.isoformat())
