"""Base entity + shared helpers for dynamic hasspy entities."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import (
    DOMAIN,
    SCOPE_DEBUG,
    STATE_ACTIVE,
    STATE_ORPHANED,
    STATE_STALE,
)

if TYPE_CHECKING:
    from .runtime import HasspyRuntime
    from .store import EntityRecord

_LOGGER = logging.getLogger(__name__)

# Attributes that change on every sweep and are useless in history: keeping
# them out of the recorder avoids churning the database with debug traffic.
_UNRECORDED = frozenset(
    {"last_seen", "lifecycle_state", "lease_ttl", "grace_seconds", "pinned"}
)

_TRUE = {"on", "true", "yes", "open", "home", "detected", "wet", "1"}
_FALSE = {"off", "false", "no", "closed", "not_home", "clear", "dry", "0"}


def truthy(value: Any) -> bool | None:
    """Best-effort bool for a free-form hasspy state."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE:
            return True
        if token in _FALSE:
            return False
    return None


def device_info_for(bridge: str, name: str, scope: str) -> DeviceInfo:
    """The device every entity of one hasspy process hangs under."""
    return DeviceInfo(
        identifiers={(DOMAIN, bridge)},
        name=name or f"hasspy {bridge}",
        manufacturer="hasspy",
        model="hasspy runtime",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://github.com/domi-devel/hasspy",
    )


class HasspyDynamicEntity(Entity):
    """An entity whose whole lifetime is described by an :class:`EntityRecord`.

    The integration is push-only: hasspy calls a service, the service mutates
    the record, and we re-render. Nothing is polled.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_unrecorded_attributes = _UNRECORDED

    def __init__(self, runtime: HasspyRuntime, record: EntityRecord) -> None:
        super().__init__()
        self.runtime = runtime
        self.record = record
        self.hass = runtime.hass
        self._attr_unique_id = record.unique_id
        self._attr_name = record.name or record.key
        self._apply_descriptors()

    # -- descriptors ---------------------------------------------------------

    def _apply_descriptors(self) -> None:
        """Copy the domain-agnostic descriptors onto the entity."""
        record = self.record
        self._attr_icon = record.icon
        if record.device_class:
            coerced = self._coerce_device_class(record.device_class)
            if coerced is not None:
                self._attr_device_class = coerced
            else:
                _LOGGER.warning(
                    "hasspy: ignoring unknown device_class %r for %s",
                    record.device_class,
                    record.unique_id,
                )
        if record.entity_category:
            try:
                self._attr_entity_category = EntityCategory(record.entity_category)
            except ValueError:
                _LOGGER.warning(
                    "hasspy: ignoring unknown entity_category %r for %s",
                    record.entity_category,
                    record.unique_id,
                )

    def _coerce_device_class(self, value: str) -> str | None:
        """Domain subclasses override to validate against their enum."""
        return value

    def refresh_descriptors(self) -> None:
        """Re-read (possibly changed) descriptors from the record."""
        self._apply_descriptors()
        self.async_write_ha_state()

    # -- identity ------------------------------------------------------------

    @property
    def device_info(self) -> DeviceInfo:
        bridge = self.record.bridge
        record = self.runtime.store.get_bridge(bridge)
        name = record.name if record and record.name else f"hasspy {bridge}"
        scope = record.scope if record else self.record.scope
        return device_info_for(bridge, name, scope)

    # -- state ---------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Debug entities go `unavailable` while stale; production never does.

        Availability is the *signal* that separates "cleaned up" from "crashed
        but still on the bridge, waiting to be re-asserted".
        """
        if self.record.scope != SCOPE_DEBUG:
            return True
        return self.record.stale_since is None

    @property
    def lifecycle_state(self) -> str:
        if self.record.stale_since is None:
            return STATE_ACTIVE
        return STATE_STALE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        record = self.record
        bridge = self.runtime.store.get_bridge(record.bridge)
        return {
            **record.attributes,
            "key": record.key,
            "bridge": record.bridge,
            "automation": record.automation,
            "scope": record.scope,
            "lifecycle_state": self.lifecycle_state,
            "pinned": record.pinned,
            "lease_ttl": record.ttl,
            "grace_seconds": bridge.grace if bridge else None,
            "last_seen": record.last_seen or None,
            "hasspy_managed": True,
        }

    # -- push from the integration ------------------------------------------

    def apply_record(self, record: EntityRecord) -> None:
        """Adopt an updated record and write the new state."""
        self.record = record
        self._apply_descriptors()
        self.async_write_ha_state()


# --- factory ----------------------------------------------------------------


def build_entity(runtime: HasspyRuntime, record: EntityRecord) -> Entity | None:
    """Instantiate the platform entity for a record's domain.

    Kept as a lazy import so platform modules can import this base class
    without a cycle.
    """
    from .binary_sensor import HasspyBinarySensor
    from .sensor import HasspySensor

    builders = {
        "sensor": HasspySensor,
        "binary_sensor": HasspyBinarySensor,
    }
    cls = builders.get(record.domain)
    if cls is None:
        _LOGGER.error(
            "hasspy: unsupported dynamic domain %r for %s",
            record.domain,
            record.unique_id,
        )
        return None
    return cls(runtime, record)
