"""Fixed entities describing one hasspy bridge.

Unlike the dynamic entities, these always exist once a bridge has registered:
they answer "is this hasspy process alive, and what is it running?".
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import device_info_for

if TYPE_CHECKING:
    from .runtime import HasspyRuntime
    from .store import BridgeRecord


def _automation_key(entry: dict[str, Any]) -> str:
    return str(entry.get("id") or entry.get("cls") or "automation")


class _BridgeEntity:
    """Shared plumbing: device + record lookup + online computation."""

    runtime: HasspyRuntime
    _bridge: str

    def __init__(self, runtime: HasspyRuntime, bridge: str) -> None:
        super().__init__()
        self.runtime = runtime
        self._bridge = bridge

    @property
    def _record(self) -> BridgeRecord | None:
        return self.runtime.store.get_bridge(self._bridge)

    @property
    def device_info(self):
        record = self._record
        return device_info_for(
            self._bridge,
            record.name if record else "",
            record.scope if record else "production",
        )

    @property
    def _bridge_online(self) -> bool:
        record = self._record
        if record is None or record.last_seen <= 0:
            return False
        return (time.time() - record.last_seen) <= record.stale_after


class BridgeOnlineEntity(_BridgeEntity, BinarySensorEntity):
    """Whether the hasspy process has heartbeated recently."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "bridge_online"

    def __init__(self, runtime: HasspyRuntime, bridge: str) -> None:
        _BridgeEntity.__init__(self, runtime, bridge)
        self._attr_unique_id = f"{bridge}|__bridge|online"
        self._attr_name = "Bridge online"
        # Explicit, stable entity_id: not derived from the (user-renamable)
        # device name, so hasspy dashboards keep working across renames.
        self.entity_id = f"binary_sensor.{bridge}_bridge_online"

    @property
    def is_on(self) -> bool:
        return self._bridge_online

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        record = self._record
        if record is None:
            return {}
        return {
            "run_id": record.run_id,
            "last_seen": record.last_seen or None,
            "stale_after": record.stale_after,
            "automations_loaded": len(record.automations),
        }


class AutomationsSensor(_BridgeEntity, SensorEntity):
    """How many automation instances the bridge reports as loaded."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:robot"

    def __init__(self, runtime: HasspyRuntime, bridge: str) -> None:
        _BridgeEntity.__init__(self, runtime, bridge)
        self._attr_unique_id = f"{bridge}|__bridge|automations"
        self._attr_name = "Automations"
        self.entity_id = f"sensor.{bridge}_automations"

    @property
    def native_value(self) -> int:
        record = self._record
        return len(record.automations) if record else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        record = self._record
        if record is None:
            return {}
        automations = record.automations
        return {
            "automations": [
                {
                    "id": _automation_key(a),
                    "cls": a.get("cls"),
                    "loaded": a.get("loaded", True),
                    "last_event": a.get("last_event"),
                    "errors": a.get("errors", 0),
                }
                for a in automations
            ],
            "ids": [_automation_key(a) for a in automations],
            "scope": record.scope,
        }
