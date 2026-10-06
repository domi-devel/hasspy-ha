"""Home-Assistant-facing wrapper around the pure model in `lifecycle.py`.

`lifecycle.py` has the dataclasses and the state machine; this module adds the
debounced `Store` persistence used by the running integration.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .const import DOMAIN, SAVE_DELAY_SECONDS, STORAGE_VERSION
from .lifecycle import BridgeRecord, EntityRecord, build_unique_id  # noqa: F401

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "BridgeRecord",
    "EntityRecord",
    "HasspyStore",
    "build_unique_id",
]


class HasspyStore:
    """Load/save and in-memory access to the persistent model."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.dynamic"
        )
        self.bridges: dict[str, BridgeRecord] = {}
        self.entities: dict[str, EntityRecord] = {}
        self._cancel_save = None

    # -- lifecycle -----------------------------------------------------------

    async def async_load(self) -> None:
        raw = await self._store.async_load() or {}
        self.bridges = {
            b["bridge"]: BridgeRecord.from_dict(b)
            for b in raw.get("bridges", [])
            if "bridge" in b
        }
        self.entities = {
            e["unique_id"]: EntityRecord.from_dict(e)
            for e in raw.get("entities", [])
            if "unique_id" in e
        }
        _LOGGER.debug(
            "hasspy store loaded: %d bridge(s), %d entity(ies)",
            len(self.bridges),
            len(self.entities),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "bridges": [b.to_dict() for b in self.bridges.values()],
            "entities": [e.to_dict() for e in self.entities.values()],
        }

    @callback
    def async_schedule_save(self) -> None:
        """Debounced persist; safe to call on every state write."""
        if self._cancel_save is not None:
            return
        self._cancel_save = async_call_later(
            self.hass, SAVE_DELAY_SECONDS, self._async_do_save
        )

    async def _async_do_save(self, _now: Any = None) -> None:
        self._cancel_save = None
        await self._store.async_save(self.as_dict())

    async def async_flush(self) -> None:
        if self._cancel_save is not None:
            self._cancel_save()
            self._cancel_save = None
        await self._store.async_save(self.as_dict())

    async def async_remove(self) -> None:
        """Delete the on-disk store (config entry removal)."""
        if self._cancel_save is not None:
            self._cancel_save()
            self._cancel_save = None
        await self._store.async_remove()

    # -- bridges -------------------------------------------------------------

    def get_bridge(self, bridge: str) -> BridgeRecord | None:
        return self.bridges.get(bridge)

    def ensure_bridge(
        self,
        bridge: str,
        *,
        name: str | None = None,
        scope: str | None = None,
        run_id: str | None = None,
    ) -> BridgeRecord:
        record = self.bridges.get(bridge)
        if record is None:
            record = BridgeRecord(bridge=bridge)
            self.bridges[bridge] = record
        if name is not None:
            record.name = name
        if scope is not None:
            record.scope = scope
        if run_id is not None:
            record.run_id = run_id
        return record

    def remove_bridge(self, bridge: str) -> None:
        self.bridges.pop(bridge, None)

    # -- entities ------------------------------------------------------------

    def get_entity(
        self, bridge: str, automation: str | None, key: str
    ) -> EntityRecord | None:
        return self.entities.get(build_unique_id(bridge, automation, key))

    def put_entity(self, record: EntityRecord) -> None:
        self.entities[record.unique_id] = record

    def drop_entity(self, unique_id: str) -> EntityRecord | None:
        return self.entities.pop(unique_id, None)

    def entities_for_bridge(self, bridge: str) -> list[EntityRecord]:
        return [e for e in self.entities.values() if e.bridge == bridge]
