"""Service handlers: the only channel hasspy needs to talk to this integration.

All services are registered in ``async_setup`` (per HA's rule that services
must exist even with no loaded config entry) and fetch the single runtime from
``hass.data``. A missing runtime raises a clear error rather than silently
no-ops, because a hasspy call that "succeeds" but creates nothing is the worst
possible failure mode for an automation runtime.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError

from .const import (
    DEFAULT_GRACE_SECONDS,
    DEFAULT_LEASE_SECONDS,
    DOMAIN,
    DYNAMIC_DOMAINS,
    SCOPE_DEBUG,
    SCOPE_PRODUCTION,
    SCOPES,
    SERVICE_CREATE_ENTITY,
    SERVICE_DELETE_ENTITY,
    SERVICE_HEARTBEAT,
    SERVICE_LIST_BRIDGES,
    SERVICE_LIST_ENTITIES,
    SERVICE_PIN,
    SERVICE_REGISTER_BRIDGE,
    SERVICE_RELEASE_SESSION,
    SERVICE_REMOVE_BRIDGE,
    SERVICE_SET_ENTITY_STATE,
    SERVICE_SWEEP,
    SERVICE_UNPIN,
)
from .runtime import HasspyRuntime
from .lifecycle import note_bridge_seen
from .store import EntityRecord, build_unique_id

_LOGGER = logging.getLogger(__name__)


def _runtime(hass: HomeAssistant) -> HasspyRuntime:
    runtimes: dict[str, HasspyRuntime] = hass.data.get(DOMAIN, {})
    if not runtimes:
        raise ServiceValidationError(
            "The hasspy integration is not set up. Add it under "
            "Settings > Devices & Services first."
        )
    return next(iter(runtimes.values()))


def _require_bridge(runtime: HasspyRuntime, bridge: str):
    record = runtime.store.get_bridge(bridge)
    if record is None:
        raise ServiceValidationError(
            f"Unknown hasspy bridge {bridge!r}. Call hasspy.register_bridge first."
        )
    return record


# --- schemas (voluptuous; HA's cv helpers are still the supported form) -----

REGISTER_BRIDGE_SCHEMA = vol.Schema(
    {
        vol.Required("bridge"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Optional("name"): str,
        vol.Optional("scope", default=SCOPE_PRODUCTION): vol.In(SCOPES),
        vol.Optional("run_id"): str,
        vol.Optional("ttl", default=DEFAULT_LEASE_SECONDS): vol.Coerce(float),
        vol.Optional("grace", default=DEFAULT_GRACE_SECONDS): vol.Coerce(float),
        vol.Optional("stale_after"): vol.Coerce(float),
        vol.Optional("automations"): list,
    }
)

HEARTBEAT_SCHEMA = vol.Schema(
    {
        vol.Required("bridge"): str,
        vol.Optional("run_id"): str,
        vol.Optional("automations"): list,
    }
)

REMOVE_BRIDGE_SCHEMA = vol.Schema(
    {
        vol.Required("bridge"): str,
        vol.Optional("delete_entities", default=False): bool,
    }
)

CREATE_ENTITY_SCHEMA = vol.Schema(
    {
        vol.Required("bridge"): str,
        vol.Optional("automation"): vol.Any(str, None),
        vol.Required("key"): vol.All(str, vol.Length(min=1, max=128)),
        vol.Required("domain"): vol.In(DYNAMIC_DOMAINS),
        vol.Optional("name"): str,
        vol.Optional("object_id"): str,
        vol.Optional("device_class"): vol.Any(str, None),
        vol.Optional("unit"): vol.Any(str, None),
        vol.Optional("state_class"): vol.Any(str, None),
        vol.Optional("icon"): vol.Any(str, None),
        vol.Optional("entity_category"): vol.Any(str, None),
        vol.Optional("attributes", default=dict): dict,
        vol.Optional("state"): vol.Any(str, int, float, bool, None),
        # No default: a missing scope inherits the bridge's scope.
        vol.Optional("scope"): vol.In(SCOPES),
        vol.Optional("ttl"): vol.Coerce(float),
        vol.Optional("pin", default=False): bool,
    }
)

SET_ENTITY_STATE_SCHEMA = vol.Schema(
    {
        vol.Required("bridge"): str,
        vol.Optional("automation"): vol.Any(str, None),
        vol.Required("key"): str,
        vol.Optional("state"): vol.Any(str, int, float, bool, None),
        vol.Optional("attributes"): dict,
    }
)

DELETE_ENTITY_SCHEMA = vol.Schema(
    {
        vol.Required("bridge"): str,
        vol.Optional("automation"): vol.Any(str, None),
        vol.Required("key"): str,
        vol.Optional("purge", default=True): bool,
    }
)

LIST_BRIDGES_SCHEMA = vol.Schema({})

LIST_ENTITIES_SCHEMA = vol.Schema(
    {
        vol.Optional("bridge"): str,
        vol.Optional("automation"): str,
        vol.Optional("scope"): vol.In(SCOPES),
    }
)

RELEASE_SESSION_SCHEMA = vol.Schema(
    {
        vol.Required("bridge"): str,
        vol.Optional("automation"): str,
        vol.Optional("run_id"): str,
    }
)

SWEEP_SCHEMA = vol.Schema({vol.Optional("dry_run", default=False): bool})

SESSION_SCHEMA = vol.Schema(
    {
        vol.Required("bridge"): str,
        vol.Optional("automation"): str,
    }
)


# --- handlers ---------------------------------------------------------------


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register every hasspy service exactly once."""

    def with_runtime(call: ServiceCall):
        return _runtime(hass)

    async def handle_register_bridge(call: ServiceCall) -> None:
        runtime = with_runtime(call)
        bridge = call.data["bridge"]
        record = runtime.store.ensure_bridge(
            bridge,
            name=call.data.get("name"),
            scope=call.data.get("scope"),
            run_id=call.data.get("run_id"),
        )
        record.ttl = call.data.get("ttl", record.ttl)
        record.grace = call.data.get("grace", record.grace)
        if call.data.get("stale_after") is not None:
            record.stale_after = call.data["stale_after"]
        if call.data.get("automations") is not None:
            record.automations = list(call.data["automations"])
        if not record.registered_at:
            record.registered_at = time.time()
        note_bridge_seen(record, time.time())
        runtime.store.async_schedule_save()
        runtime.async_sync_bridge(bridge)

    async def handle_heartbeat(call: ServiceCall) -> None:
        runtime = with_runtime(call)
        bridge = call.data["bridge"]
        record = runtime.store.get_bridge(bridge)
        if record is None:
            # Self-healing: a heartbeat for an unknown bridge re-registers it,
            # so a bridge whose registration call was lost still appears.
            record = runtime.store.ensure_bridge(
                bridge, run_id=call.data.get("run_id")
            )
        note_bridge_seen(record, time.time())
        if call.data.get("run_id"):
            record.run_id = call.data["run_id"]
        if call.data.get("automations") is not None:
            record.automations = list(call.data["automations"])
        runtime.store.async_schedule_save()
        runtime.async_sync_bridge(bridge)

    async def handle_remove_bridge(call: ServiceCall) -> None:
        runtime = with_runtime(call)
        await runtime.async_remove_bridge(
            call.data["bridge"],
            delete_entities=bool(call.data.get("delete_entities")),
        )
        runtime.store.async_schedule_save()

    async def handle_create_entity(call: ServiceCall) -> ServiceResponse:
        runtime = with_runtime(call)
        bridge = call.data["bridge"]
        record_bridge = _require_bridge(runtime, bridge)
        automation = call.data.get("automation")
        key = call.data["key"]
        unique_id = build_unique_id(bridge, automation, key)

        existing = runtime.store.get_entity(bridge, automation, key)
        if existing is not None and existing.domain != call.data["domain"]:
            raise ServiceValidationError(
                f"Entity {unique_id!r} already exists as domain "
                f"{existing.domain!r}; delete it before recreating as "
                f"{call.data['domain']!r}."
            )

        # Default the entity's scope to the bridge's, so a `scope: debug`
        # bridge does not have to repeat `scope: debug` on every create.
        scope = call.data.get("scope") or record_bridge.scope
        record = existing or EntityRecord(
            unique_id=unique_id,
            bridge=bridge,
            key=key,
            domain=call.data["domain"],
            created_at=time.time(),
        )
        record.automation = automation
        record.scope = scope
        if call.data.get("name") is not None:
            record.name = call.data["name"]
        record.object_id = call.data.get("object_id") or record.object_id
        record.device_class = call.data.get("device_class")
        record.unit = call.data.get("unit")
        record.state_class = call.data.get("state_class")
        record.icon = call.data.get("icon")
        record.entity_category = call.data.get("entity_category")
        record.attributes = dict(call.data.get("attributes") or {})
        record.initial_state = call.data.get("state")
        if existing is None or call.data.get("state") is not None:
            record.state = call.data.get("state")

        # Lifetime for debug entities: inherit the bridge defaults, allow an
        # explicit pin so a developer can keep a dump indefinitely.
        if scope == SCOPE_DEBUG:
            record.ttl = call.data.get("ttl", record_bridge.ttl)
            record.pinned = bool(call.data.get("pin"))
            record.persist = True
        else:
            record.ttl = 0.0
            record.pinned = False
            record.persist = True
        runtime.store.put_entity(record)
        runtime.async_mark_seen(record)
        runtime.upsert(record)
        runtime.store.async_schedule_save()

        if existing is None:
            # async_add_entities schedules the add; let it settle so the entity
            # registry has the entity_id before we report it back. Only the
            # first create pays this cost.
            await hass.async_block_till_done()

        entity = runtime.get_entity(unique_id)
        entity_id = getattr(entity, "entity_id", None)
        if entity_id is None:
            entity_id = _resolve_entity_id(hass, record)
        return {
            "entity_id": entity_id,
            "unique_id": unique_id,
            "scope": scope,
            "ttl": record.ttl,
            "grace": record_bridge.grace if scope == SCOPE_DEBUG else None,
        }

    async def handle_set_entity_state(call: ServiceCall) -> None:
        runtime = with_runtime(call)
        bridge = call.data["bridge"]
        automation = call.data.get("automation")
        key = call.data["key"]
        record = runtime.store.get_entity(bridge, automation, key)
        if record is None:
            raise ServiceValidationError(
                f"Unknown hasspy entity {build_unique_id(bridge, automation, key)!r}. "
                "Call hasspy.create_entity first."
            )
        if "state" in call.data:
            record.state = call.data["state"]
        if call.data.get("attributes") is not None:
            record.attributes = dict(call.data["attributes"])
        runtime.async_mark_seen(record)
        runtime.upsert(record)
        runtime.store.async_schedule_save()

    async def handle_delete_entity(call: ServiceCall) -> None:
        runtime = with_runtime(call)
        record = runtime.store.get_entity(
            call.data["bridge"], call.data.get("automation"), call.data["key"]
        )
        if record is None:
            return
        if call.data.get("purge", True):
            await runtime.async_remove_entity(record.unique_id)
        runtime.store.drop_entity(record.unique_id)
        runtime.store.async_schedule_save()

    async def handle_list_bridges(call: ServiceCall) -> ServiceResponse:
        runtime = with_runtime(call)
        now = time.time()
        return {
            "bridges": [
                {
                    **b.to_dict(),
                    "online": (now - b.last_seen) <= b.stale_after
                    if b.last_seen
                    else False,
                }
                for b in runtime.store.bridges.values()
            ]
        }

    async def handle_list_entities(call: ServiceCall) -> ServiceResponse:
        runtime = with_runtime(call)
        wanted_bridge = call.data.get("bridge")
        wanted_automation = call.data.get("automation")
        wanted_scope = call.data.get("scope")
        out = []
        for record in runtime.store.entities.values():
            if wanted_bridge and record.bridge != wanted_bridge:
                continue
            if wanted_automation and record.automation != wanted_automation:
                continue
            if wanted_scope and record.scope != wanted_scope:
                continue
            entity = runtime.get_entity(record.unique_id)
            out.append(
                {
                    "unique_id": record.unique_id,
                    "entity_id": getattr(entity, "entity_id", None),
                    "bridge": record.bridge,
                    "automation": record.automation,
                    "key": record.key,
                    "domain": record.domain,
                    "scope": record.scope,
                    "state": record.state,
                    "stale": record.stale_since is not None,
                    "pinned": record.pinned,
                    "ttl": record.ttl,
                    "last_seen": record.last_seen,
                }
            )
        return {"entities": out}

    async def handle_release_session(call: ServiceCall) -> ServiceResponse:
        """Collect every debug entity of a bridge (optionally one automation).

        This is the graceful path: a debugging run that exits cleanly calls it
        and its entities disappear at once, without waiting for any TTL.
        """
        runtime = with_runtime(call)
        bridge = call.data["bridge"]
        automation = call.data.get("automation")
        removed = []
        for record in list(runtime.store.entities.values()):
            if record.bridge != bridge or record.scope != SCOPE_DEBUG:
                continue
            if automation is not None and record.automation != automation:
                continue
            await runtime.async_remove_entity(record.unique_id)
            runtime.store.drop_entity(record.unique_id)
            removed.append(record.unique_id)
        runtime.store.async_schedule_save()
        return {"released": removed, "count": len(removed)}

    async def handle_sweep(call: ServiceCall) -> ServiceResponse:
        runtime = with_runtime(call)
        if call.data.get("dry_run"):
            from .debug import evaluate

            now = time.time()
            verdicts = {
                r.unique_id: evaluate(runtime.store, r, now)
                for r in runtime.store.entities.values()
                if r.scope == SCOPE_DEBUG and not r.pinned
            }
            return {"dry_run": True, "verdicts": verdicts}
        return {"dry_run": False, "counts": await runtime.collector.async_sweep()}

    async def handle_pin(call: ServiceCall) -> None:
        _async_set_pin(hass, call, True)

    async def handle_unpin(call: ServiceCall) -> None:
        _async_set_pin(hass, call, False)

    hass.services.async_register(
        DOMAIN, SERVICE_REGISTER_BRIDGE, handle_register_bridge, REGISTER_BRIDGE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_HEARTBEAT, handle_heartbeat, HEARTBEAT_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE_BRIDGE, handle_remove_bridge, REMOVE_BRIDGE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CREATE_ENTITY,
        handle_create_entity,
        CREATE_ENTITY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_ENTITY_STATE, handle_set_entity_state, SET_ENTITY_STATE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_ENTITY, handle_delete_entity, DELETE_ENTITY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_BRIDGES,
        handle_list_bridges,
        LIST_BRIDGES_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_ENTITIES,
        handle_list_entities,
        LIST_ENTITIES_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RELEASE_SESSION,
        handle_release_session,
        RELEASE_SESSION_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SWEEP,
        handle_sweep,
        SWEEP_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(DOMAIN, SERVICE_PIN, handle_pin, SESSION_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_UNPIN, handle_unpin, SESSION_SCHEMA)


@callback
def _async_set_pin(hass: HomeAssistant, call: ServiceCall, pinned: bool) -> None:
    """Pin/unpin every debug entity matching bridge (+ optional automation).

    ``hasspy.pin`` is a lifecycle *action*, not a state write: it uses the
    session schema (bridge + optional automation) rather than a key, because
    the usual intent is "keep everything this debugging run created".
    """
    runtime = _runtime(hass)
    bridge = call.data["bridge"]
    automation = call.data.get("automation")
    for record in runtime.store.entities.values():
        if record.bridge != bridge or record.scope != SCOPE_DEBUG:
            continue
        if automation is not None and record.automation != automation:
            continue
        record.pinned = pinned
        if pinned and record.stale_since is not None:
            # Pinning an entity also revives it: the developer explicitly asked
            # to keep it, so it should not sit at `unavailable`.
            record.stale_since = None
        entity = runtime.get_entity(record.unique_id)
        if entity is not None:
            entity.apply_record(record)
    runtime.store.async_schedule_save()


@callback
def _resolve_entity_id(hass: HomeAssistant, record: EntityRecord) -> str | None:
    """Best-effort entity_id lookup for a freshly added entity.

    ``async_add_entities`` schedules the add on the event loop, so the
    entity_id may not be available in the same tick. We look it up from the
    entity registry by unique_id, which is populated as soon as the entity is
    added; on a miss the caller simply gets ``None`` and can call
    ``list_entities`` later.
    """
    from homeassistant.helpers import entity_registry as er

    return er.async_get(hass).async_get_entity_id(
        record.domain, DOMAIN, record.unique_id
    )
