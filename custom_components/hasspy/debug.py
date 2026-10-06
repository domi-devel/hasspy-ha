"""Debug-entity collector: drives the pure state machine in `lifecycle.py`.

See ``docs/DEBUG_ENTITIES.md`` for the concept; this module only wires the
periodic sweep to Home Assistant and applies the transitions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import SCOPE_DEBUG, SWEEP_INTERVAL_SECONDS
from .lifecycle import evaluate, now_seconds, should_revive

if TYPE_CHECKING:
    from .runtime import HasspyRuntime

_LOGGER = logging.getLogger(__name__)


class DebugCollector:
    """Periodic sweep plus the transitions it drives."""

    def __init__(self, hass: HomeAssistant, runtime: HasspyRuntime) -> None:
        self.hass = hass
        self.runtime = runtime
        self._cancel = None

    @callback
    def start(self) -> None:
        from datetime import timedelta

        self._cancel = async_track_time_interval(
            self.hass, self._async_tick, timedelta(seconds=SWEEP_INTERVAL_SECONDS)
        )

    @callback
    def stop(self) -> None:
        if self._cancel is not None:
            self._cancel()
            self._cancel = None

    async def _async_tick(self, _now) -> None:
        await self.async_sweep()

    async def async_sweep(self) -> dict[str, int]:
        """One garbage-collection pass. Returns counts, for tests/logging."""
        now = now_seconds()
        store = self.runtime.store
        counts = {"stale": 0, "revived": 0, "collected": 0, "kept": 0}

        for record in list(store.entities.values()):
            if record.scope != SCOPE_DEBUG or record.pinned:
                counts["kept"] += 1
                continue

            verdict = evaluate(store, record, now)
            if verdict == "collect":
                await self.runtime.async_remove_entity(record.unique_id)
                store.drop_entity(record.unique_id)
                counts["collected"] += 1
            elif verdict == "stale":
                if record.stale_since is None:
                    record.stale_since = now
                    bridge = store.get_bridge(record.bridge)
                    _LOGGER.info(
                        "hasspy: debug entity %s went stale (bridge=%s, ttl=%.0fs, "
                        "grace=%.0fs)",
                        record.unique_id,
                        record.bridge,
                        record.ttl,
                        bridge.grace if bridge else 0.0,
                    )
                counts["stale"] += 1
                entity = self.runtime.get_entity(record.unique_id)
                if entity is not None:
                    entity.async_write_ha_state()
            else:
                counts["kept"] += 1

        counts["revived"] = self._revive_sweep(now)
        if counts["collected"] or counts["stale"] or counts["revived"]:
            store.async_schedule_save()
            self._notify_bridge_entities()
        return counts

    @callback
    def _revive_sweep(self, now: float) -> int:
        """Clear stale_since for records that were re-asserted by their owner."""
        store = self.runtime.store
        revived = 0
        for record in store.entities.values():
            if not should_revive(store, record, now):
                continue
            record.stale_since = None
            entity = self.runtime.get_entity(record.unique_id)
            if entity is not None:
                entity.async_write_ha_state()
            revived += 1
        return revived

    @callback
    def _notify_bridge_entities(self) -> None:
        for bridge in list(self.runtime.store.bridges):
            self.runtime.async_sync_bridge(bridge)
