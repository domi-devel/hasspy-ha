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
from datetime import timedelta
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
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_GRACE_SECONDS,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_RULE_EXPIRY_SECONDS,
    DOMAIN,
    DYNAMIC_DOMAINS,
    SCOPE_DEBUG,
    SCOPE_PRODUCTION,
    SCOPES,
    SERVICE_ADD_RULE,
    SERVICE_CANCEL_RULE,
    SERVICE_CREATE_ENTITY,
    SERVICE_DELETE_ENTITY,
    SERVICE_HEARTBEAT,
    SERVICE_LIST_BRIDGES,
    SERVICE_LIST_ENTITIES,
    SERVICE_LIST_RULES,
    SERVICE_LOG,
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
from .rules import RuleError, parse_relative_offset
from .store import EntityRecord, build_unique_id

_LOGGER = logging.getLogger(__name__)

# How many log lines to keep per bridge (newest first).
MAX_LOG_ENTRIES = 100


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
        # Control descriptors, used when domain is number/select/switch/datetime.
        vol.Optional("min"): vol.Coerce(float),
        vol.Optional("max"): vol.Coerce(float),
        vol.Optional("step"): vol.Coerce(float),
        vol.Optional("mode"): vol.In(("auto", "box", "slider")),
        vol.Optional("options"): [str],
        vol.Optional("has_date"): bool,
        vol.Optional("has_time"): bool,
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

LOG_SCHEMA = vol.Schema(
    {
        vol.Required("bridge"): str,
        vol.Required("message"): vol.All(str, vol.Length(max=2000)),
        vol.Optional("automation"): vol.Any(str, None),
        vol.Optional("level", default="info"): vol.In(
            ("debug", "info", "warning", "error")
        ),
        vol.Optional("logbook", default=True): bool,
    }
)

# --- one-shot rules --------------------------------------------------------
#
# `trigger` is a raw HA trigger config (any platform); `at`/`in` are the
# point-in-time shortcuts for "at 12:00" / "in 5 minutes". Exactly one of the
# two trigger flavours is required -- enforced in the handler so the error
# message can say which one is missing.
ADD_RULE_SCHEMA = vol.Schema(
    {
        vol.Required("name"): vol.All(str, vol.Length(min=1, max=120)),
        vol.Required("action"): dict,
        vol.Optional("trigger"): dict,
        vol.Optional("at"): str,
        vol.Optional("in"): vol.Any(str, vol.Coerce(float)),
        vol.Optional("bridge"): str,
        vol.Optional("key"): str,
        vol.Optional("allow_any_action", default=False): bool,
        vol.Optional("fire_now_if_true", default=False): bool,
        vol.Optional("expires_in", default=DEFAULT_RULE_EXPIRY_SECONDS): vol.Any(
            None, vol.Coerce(float)
        ),
    }
)

CANCEL_RULE_SCHEMA = vol.Schema(
    {
        vol.Required("rule_id"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Optional("reason"): str,
        vol.Optional("forget", default=False): bool,
    }
)

LIST_RULES_SCHEMA = vol.Schema(
    {
        vol.Optional("bridge"): str,
        vol.Optional("active_only", default=False): bool,
        vol.Optional("include_finished", default=True): bool,
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

    async def handle_log(call: ServiceCall) -> None:
        """Record a log line from hasspy automations.

        Stored in a per-bridge ring buffer (readable as sensor.<bridge>_last_log
        and its `log` attribute), mirrored to the HA system log at the matching
        level, and optionally written to the logbook so it shows in the UI on a
        timeline.
        """
        runtime = with_runtime(call)
        bridge = call.data["bridge"]
        record = runtime.store.get_bridge(bridge)
        if record is None:
            # Self-heal like heartbeat: a log before register still lands.
            record = runtime.store.ensure_bridge(bridge)

        entry = {
            "time": time.time(),
            "automation": call.data.get("automation"),
            "level": call.data["level"],
            "message": call.data["message"],
        }
        record.log.insert(0, entry)
        del record.log[MAX_LOG_ENTRIES:]

        level = call.data["level"]
        text = f"[{bridge}]"
        if entry["automation"]:
            text += f" [{entry['automation']}]"
        text += f" {entry['message']}"
        {"debug": _LOGGER.debug, "info": _LOGGER.info,
         "warning": _LOGGER.warning, "error": _LOGGER.error}[level](text)

        if call.data.get("logbook") and hass.services.has_service("logbook", "log"):
            hass.async_create_task(
                hass.services.async_call(
                    "logbook",
                    "log",
                    {
                        "name": bridge,
                        "message": call.data["message"],
                        "entity_id": f"sensor.{bridge}_last_log",
                    },
                    blocking=False,
                )
            )

        runtime.store.async_schedule_save()
        runtime.async_sync_bridge(bridge)

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
        # Descriptors are only overwritten when supplied. `create_entity` is
        # also the "publish a new value" path (see Automation.publish), so a
        # repeat call carries just the state and must not wipe the descriptor
        # it was first created with.
        for field in (
            "device_class",
            "unit",
            "state_class",
            "icon",
            "entity_category",
            "min",
            "max",
            "step",
            "mode",
            "options",
            "has_date",
            "has_time",
        ):
            if call.data.get(field) is not None:
                setattr(record, field, call.data[field])
        if call.data.get("attributes"):
            record.attributes = dict(call.data["attributes"])
        record.initial_state = call.data.get("state")
        # A *value* entity is written by hasspy, so every call sets its state.
        # A *control* is owned by the user: `state`/`value` is only the initial
        # default, applied when the entity is first created -- otherwise every
        # hasspy restart would reset the user's setting back to the declared
        # default (the class default is passed on each create_control call).
        if existing is None:
            record.state = call.data.get("state")
        elif not record.is_control and call.data.get("state") is not None:
            record.state = call.data["state"]

        # Lifetime for debug entities: inherit the bridge defaults, allow an
        # explicit pin so a developer can keep a dump indefinitely.
        # Controls are settings: always durable, never leased.
        if scope == SCOPE_DEBUG and not record.is_control:
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
        if record.is_control:
            raise ServiceValidationError(
                f"{record.unique_id!r} is a control ({record.domain}). Use "
                "hasspy.set_control, or the entity's own service in Home Assistant."
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
            # Only debug *values*: a control is a setting and must survive.
            if (
                record.bridge != bridge
                or record.scope != SCOPE_DEBUG
                or record.is_control
            ):
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

    # --- one-shot rules ---------------------------------------------------

    async def handle_add_rule(call: ServiceCall) -> ServiceResponse:
        """Arm a one-shot rule: a trigger plus the action to run once.

        Rejections come back as `ServiceValidationError` with an actionable
        message (the common one: the trigger condition is already true, so a
        Home Assistant trigger could never fire).
        """
        runtime = with_runtime(call)
        fire_at = _resolve_fire_at(call)
        trigger = call.data.get("trigger")
        try:
            record = await runtime.rule_engine.async_add(
                name=call.data["name"],
                action=call.data["action"],
                trigger=trigger,
                fire_at=fire_at,
                bridge=call.data.get("bridge"),
                key=call.data.get("key"),
                allow_any_action=bool(call.data.get("allow_any_action")),
                fire_now_if_true=bool(call.data.get("fire_now_if_true")),
                expires_in=call.data.get("expires_in"),
            )
        except RuleError as err:
            raise ServiceValidationError(str(err)) from err
        return runtime.rule_payload(record)

    async def handle_cancel_rule(call: ServiceCall) -> ServiceResponse:
        runtime = with_runtime(call)
        rule_id = call.data["rule_id"]
        record = runtime.store.get_rule(rule_id)
        if record is None:
            raise ServiceValidationError(f"Unknown hasspy rule {rule_id!r}.")
        if call.data.get("forget"):
            await runtime.rule_engine.async_forget(rule_id)
            return {"rule_id": rule_id, "forgotten": True}
        await runtime.rule_engine.async_cancel(
            rule_id, reason=call.data.get("reason") or "cancelled"
        )
        runtime.store.async_schedule_save()
        payload = runtime.rule_payload(record)
        payload["forgotten"] = False
        return payload

    async def handle_list_rules(call: ServiceCall) -> ServiceResponse:
        runtime = with_runtime(call)
        bridge = call.data.get("bridge")
        active_only = bool(call.data.get("active_only"))
        include_finished = bool(call.data.get("include_finished", True))
        out = []
        for record in runtime.store.rules.values():
            if bridge is not None and record.bridge != bridge:
                continue
            if active_only and not record.is_active:
                continue
            if not active_only and not include_finished and not record.is_active:
                continue
            out.append(runtime.rule_payload(record))
        out.sort(key=lambda r: r["created_at"], reverse=True)
        return {"rules": out, "count": len(out)}

    hass.services.async_register(
        DOMAIN, SERVICE_REGISTER_BRIDGE, handle_register_bridge, REGISTER_BRIDGE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_HEARTBEAT, handle_heartbeat, HEARTBEAT_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_LOG, handle_log, LOG_SCHEMA)
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
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_RULE,
        handle_add_rule,
        ADD_RULE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CANCEL_RULE,
        handle_cancel_rule,
        CANCEL_RULE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_RULES,
        handle_list_rules,
        LIST_RULES_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


@callback
def _resolve_fire_at(call: ServiceCall) -> float | None:
    """Turn the `at`/`in` shortcuts into an absolute UTC epoch timestamp.

    `in` accepts "300", 300 or "5m"/"2h"; `at` accepts ISO-8601 (with an offset,
    or naive = local time) or a bare ``HH:MM`` meaning the next occurrence.
    """
    offset_spec = call.data.get("in")
    at_spec = call.data.get("at")

    if offset_spec is not None:
        seconds = parse_relative_offset(str(offset_spec))
        if seconds is None:
            raise ServiceValidationError(
                f"'in' must be a duration like 300, '5m', '2h' (got {offset_spec!r})"
            )
        return (dt_util.utcnow() + timedelta(seconds=seconds)).timestamp()

    if at_spec is not None:
        text = str(at_spec).strip()
        parsed = dt_util.parse_datetime(text)
        if parsed is None:
            # A bare HH:MM means "the next time the clock reads that".
            clock = dt_util.parse_time(text)
            if clock is not None:
                now_local = dt_util.now()
                candidate = now_local.replace(
                    hour=clock.hour,
                    minute=clock.minute,
                    second=clock.second,
                    microsecond=0,
                )
                if candidate <= now_local:
                    candidate += timedelta(days=1)
                return candidate.timestamp()
            raise ServiceValidationError(
                f"'at' must be ISO-8601 or HH:MM (got {at_spec!r})"
            )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return parsed.timestamp()

    return None


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
