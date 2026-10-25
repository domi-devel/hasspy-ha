"""Entities that make one-shot rules visible and cancellable.

Deliberately separate from the dynamic entity records in `store.py`: those
describe things *hasspy* owns and leases, while a rule is owned by Home
Assistant and has a lifecycle (armed -> fired/cancelled/expired). Each rule gets

* a `sensor` showing its state (armed/fired/...) with the trigger and action as
  attributes, and
* a `button` that cancels it.

Both hang under one dedicated device, so rules do not clutter the bridge device
and can be found in one place.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MANIFEST_VERSION
from .rules import RuleRecord, describe_action, describe_trigger

if TYPE_CHECKING:
    from .runtime import HasspyRuntime

_LOGGER = logging.getLogger(__name__)

RULES_DEVICE_ID = "__rules"
RULES_DEVICE_NAME = "hasspy rules"

# Not recorder-worthy: churns on every sync and carries no history value.
_UNRECORDED = frozenset({"warnings"})

_STATE_ICONS = {
    "armed": "mdi:alarm",
    "fired": "mdi:alarm-check",
    "cancelled": "mdi:alarm-off",
    "expired": "mdi:alarm-minus",
    "error": "mdi:alarm-alert",
}


def rules_device_info() -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, RULES_DEVICE_ID)},
        name=RULES_DEVICE_NAME,
        manufacturer="hasspy",
        model="hasspy one-shot rules",
        entry_type=DeviceEntryType.SERVICE,
        sw_version=MANIFEST_VERSION,
        configuration_url="https://github.com/domi-devel/hasspy-ha",
    )


class _RuleEntity:
    """Shared plumbing for the per-rule entities."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_device_info = rules_device_info()

    def __init__(self, runtime: HasspyRuntime, record: RuleRecord) -> None:
        super().__init__()
        self.runtime = runtime
        self.record = record
        self.hass = runtime.hass

    @property
    def _live(self) -> RuleRecord | None:
        """Re-read the record: the engine mutates it in place."""
        return self.runtime.store.get_rule(self.record.rule_id)

    @property
    def available(self) -> bool:
        return self._live is not None


class HasspyRuleSensor(_RuleEntity, SensorEntity):
    """The state of one rule, with its trigger and action as attributes."""

    _attr_unrecorded_attributes = _UNRECORDED

    def __init__(self, runtime: HasspyRuntime, record: RuleRecord) -> None:
        super().__init__(runtime, record)
        self._attr_unique_id = f"rule|{record.rule_id}|state"
        self._attr_name = record.name
        # Reuse a registered (possibly user-renamed) entity_id, so a rename in
        # the UI survives a restart instead of being overwritten here.
        registered = er.async_get(runtime.hass).async_get_entity_id(
            "sensor", DOMAIN, self._attr_unique_id
        )
        self.entity_id = registered or f"sensor.hasspy_rule_{record.rule_id}"

    @property
    def icon(self) -> str | None:
        return _STATE_ICONS.get(self.record.state)

    @property
    def native_value(self) -> str:
        return self.record.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        record = self.record
        attrs: dict[str, Any] = {
            "rule_id": record.rule_id,
            "name": record.name,
            "trigger": record.trigger,
            "action": record.action,
            "trigger_description": describe_trigger(record),
            "action_description": describe_action(record),
            "armed": record.is_active,
            "kind": record.kind,
            # Lets a dashboard or a voice script find the cancel button and the
            # rule id from the sensor alone, without deriving the naming scheme.
            "cancel_entity_id": f"button.hasspy_rule_{record.rule_id}_cancel",
            "created_at": dt_util.utc_from_timestamp(record.created_at).isoformat(),
            "fired_at": (
                dt_util.utc_from_timestamp(record.fired_at).isoformat()
                if record.fired_at
                else None
            ),
            "finished_at": (
                dt_util.utc_from_timestamp(record.finished_at).isoformat()
                if record.finished_at
                else None
            ),
            "expires_at": (
                dt_util.utc_from_timestamp(record.expires_at).isoformat()
                if record.expires_at
                else None
            ),
            "action_runs": record.action_runs,
            "allow_any_action": record.allow_any_action,
            "bridge": record.bridge,
            "key": record.key,
            "warnings": list(record.warnings),
            "last_error": record.last_error,
            "cancel_reason": record.cancel_reason,
            "hasspy_managed": True,
        }
        if record.fire_at is not None:
            attrs["fire_at"] = dt_util.utc_from_timestamp(record.fire_at).isoformat()
        return attrs


class HasspyRuleCancelButton(_RuleEntity, ButtonEntity):
    """Cancel this rule. Turning a rule down is one tap (or one voice phrase)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime: HasspyRuntime, record: RuleRecord) -> None:
        super().__init__(runtime, record)
        self._attr_unique_id = f"rule|{record.rule_id}|cancel"
        self._attr_name = f"Cancel {record.name}"
        registered = er.async_get(runtime.hass).async_get_entity_id(
            "button", DOMAIN, self._attr_unique_id
        )
        self.entity_id = registered or f"button.hasspy_rule_{record.rule_id}_cancel"

    @property
    def icon(self) -> str | None:
        return "mdi:cancel"

    @property
    def available(self) -> bool:
        live = self._live
        return live is not None and live.is_active

    async def async_press(self) -> None:
        record = self._live
        if record is None:
            return
        await self.runtime.rule_engine.async_cancel(
            record.rule_id, reason="cancelled from the dashboard"
        )


def build_rule_entity(
    runtime: HasspyRuntime, record: RuleRecord, domain: str
) -> Any | None:
    """The entity of one rule for one platform, or None if that platform has none."""
    if domain == "sensor":
        return HasspyRuleSensor(runtime, record)
    if domain == "button":
        return HasspyRuleCancelButton(runtime, record)
    return None


async def async_setup_rules_platform(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    domain: str,
) -> None:
    """Register one platform and spawn the entities of every stored rule."""
    runtime = entry.runtime_data
    runtime.register_platform(domain, async_add_entities)
    runtime.async_spawn_rules(domain)


__all__ = [
    "HasspyRuleCancelButton",
    "HasspyRuleSensor",
    "async_setup_rules_platform",
    "build_rule_entity",
    "rules_device_info",
]
