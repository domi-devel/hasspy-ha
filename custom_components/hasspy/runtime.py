"""Runtime object: owns the store, the live entity map and the platforms."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .bridge import AutomationsSensor, BridgeOnlineEntity
from .const import DOMAIN, MANIFEST_VERSION
from .debug import DebugCollector
from .entity import build_entity, device_info_for
from .store import EntityRecord, HasspyStore
from .lifecycle import default_object_id

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


class HasspyRuntime:
    """Per-config-entry state shared by every platform and service."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, store: HasspyStore
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.store = store
        self.collector = DebugCollector(hass, self)
        self._platform_add: dict[str, AddEntitiesCallback] = {}
        self._entities: dict[str, Any] = {}

    @callback
    def async_start(self) -> None:
        """Called once the entry is loaded: start the GC and render bridges."""
        self.collector.start()
        for bridge in list(self.store.bridges):
            self.async_sync_bridge(bridge)

    @callback
    def async_stop(self) -> None:
        self.collector.stop()

    # -- platform plumbing ---------------------------------------------------

    @callback
    def register_platform(self, domain: str, add: AddEntitiesCallback) -> None:
        self._platform_add[domain] = add
        self.async_ensure_bridge_entities(domain)

    @callback
    def async_ensure_bridge_entities(self, domain: str) -> None:
        """Create the fixed presence entities for every known bridge."""
        for bridge in list(self.store.bridges):
            self.async_ensure_device(bridge)
            if domain == "binary_sensor":
                self._get_or_build(
                    domain,
                    f"{bridge}|__bridge|online",
                    lambda b=bridge: BridgeOnlineEntity(self, b),
                )
            elif domain == "sensor":
                self._get_or_build(
                    domain,
                    f"{bridge}|__bridge|automations",
                    lambda b=bridge: AutomationsSensor(self, b),
                )

    async def async_spawn_stored(self, domain: str) -> None:
        """(Re-)create the persisted dynamic entities of one platform."""
        for record in list(self.store.entities.values()):
            if record.domain != domain or record.unique_id in self._entities:
                continue
            self._async_add(record)
        self.async_ensure_bridge_entities(domain)

    # -- bridge/device -------------------------------------------------------

    @callback
    def async_ensure_device(self, bridge: str) -> None:
        record = self.store.get_bridge(bridge)
        info = device_info_for(
            bridge,
            record.name if record else "",
            record.scope if record else "production",
        )
        dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers=info["identifiers"],
            name=info.get("name"),
            manufacturer=info.get("manufacturer"),
            model=info.get("model"),
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=info.get("configuration_url"),
            sw_version=MANIFEST_VERSION,
        )

    @callback
    def async_sync_bridge(self, bridge: str) -> None:
        """Refresh the presence entities after a register/heartbeat."""
        self.async_ensure_device(bridge)
        for unique_id in (f"{bridge}|__bridge|online", f"{bridge}|__bridge|automations"):
            entity = self._entities.get(unique_id)
            if entity is not None:
                entity.async_write_ha_state()
        self.async_ensure_bridge_entities("binary_sensor")
        self.async_ensure_bridge_entities("sensor")

    # -- dynamic entities ----------------------------------------------------

    def get_entity(self, unique_id: str) -> Any:
        return self._entities.get(unique_id)

    @callback
    def _get_or_build(self, domain: str, unique_id: str, factory) -> Any:
        entity = self._entities.get(unique_id)
        if entity is not None:
            return entity
        add = self._platform_add.get(domain)
        if add is None:
            return None
        entity = factory()
        if entity is None:
            return None
        self._entities[unique_id] = entity
        add([entity])
        return entity

    @callback
    def _async_add(self, record: EntityRecord) -> None:
        if record.unique_id in self._entities:
            return
        add = self._platform_add.get(record.domain)
        if add is None:
            # Platform not forwarded yet; async_spawn_stored will pick it up.
            return
        entity = build_entity(self, record)
        if entity is None:
            return
        # Deterministic entity_id: explicit object_id wins, else a stable
        # bridge/automation/key slug. Never the device name (user-renamable).
        # If the entity is already registered we reuse its (possibly renamed)
        # entity_id so a user rename survives reloads.
        ent_reg = er.async_get(self.hass)
        registered = ent_reg.async_get_entity_id(
            record.domain, DOMAIN, record.unique_id
        )
        if registered:
            entity.entity_id = registered
        else:
            object_id = record.object_id or default_object_id(
                record.bridge, record.automation, record.key
            )
            candidate = f"{record.domain}.{object_id}"
            if self.hass.states.get(candidate) is not None:
                # A foreign entity already owns it; pick a free variant.
                candidate = ent_reg.async_get_available_entity_id(
                    record.domain, object_id
                )
            entity.entity_id = candidate
        self._entities[record.unique_id] = entity
        add([entity])

    @callback
    def upsert(self, record: EntityRecord) -> None:
        """Create or update the live entity for a record."""
        entity = self._entities.get(record.unique_id)
        if entity is None:
            self._async_add(record)
            return
        entity.apply_record(record)

    async def async_remove_entity(self, unique_id: str) -> bool:
        entity = self._entities.pop(unique_id, None)
        if entity is None:
            return False
        entity_id = getattr(entity, "entity_id", None)
        await entity.async_remove()
        if entity_id:
            er.async_get(self.hass).async_remove(entity_id)
        return True

    async def async_remove_bridge(self, bridge: str, *, delete_entities: bool) -> None:
        """Forget a bridge, tearing down everything that belongs to it.

        Removing the device removes its fixed presence entities (and, through
        the device registry, any entity still attached to it). Dynamic entities
        are removed explicitly first so their registry entries go too.
        """
        if delete_entities:
            for record in list(self.store.entities_for_bridge(bridge)):
                await self.async_remove_entity(record.unique_id)
                self.store.drop_entity(record.unique_id)

        # Fixed presence entities are not in the store, so drop them by key.
        await self.async_remove_entity(f"{bridge}|__bridge|online")
        await self.async_remove_entity(f"{bridge}|__bridge|automations")

        self.store.remove_bridge(bridge)

        # Drop the device last: this also clears anything still attached to it.
        # Look it up via the config entry (async_get_device is deprecated in
        # HA 2026.9+ since identifiers are no longer globally unique).
        device_registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(
            device_registry, self.entry.entry_id
        ):
            if (DOMAIN, bridge) in device.identifiers:
                device_registry.async_remove_device(device.id)
                break

    @callback
    def async_mark_seen(self, record: EntityRecord, now: float | None = None) -> None:
        record.last_seen = now if now is not None else time.time()
        record.stale_since = None
