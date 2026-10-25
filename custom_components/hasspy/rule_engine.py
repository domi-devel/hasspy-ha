"""The one-shot rule engine: attach a trigger, run an action, then retire.

Rules are *not* automations: an automation is a permanent entity whose triggers
are always attached, while a rule is armed once, fires once, and is finished.
That difference is why this module exists instead of telling users to write an
automation with a `condition` - a rule is created, listed and cancelled from
Home Assistant (and by voice) without editing YAML.

The two building blocks are Home Assistant's own, so behaviour always matches
what an automation would do:

* triggers - ``async_validate_trigger_config`` + ``async_initialize_triggers``
  (exactly what ``automation/__init__.py`` calls), so every trigger platform is
  supported for free.
* actions - ``helpers.script.Script``, given a sequence built by
  ``rules.action_script_sequence``.

Two semantics are inherited and are worth knowing (see docs/RULES.md):

* an entity trigger fires on a *transition*, never on attach, and
* a pending ``for:`` duration restarts its clock after a restart.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.core import CALLBACK_TYPE, Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_time_interval,
)
from homeassistant.helpers.script import Script
from homeassistant.helpers.trigger import (
    async_initialize_triggers,
    async_validate_trigger_config,
)
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_RULE_EXPIRY_SECONDS,
    RULE_RETENTION_SECONDS,
    RULE_STATE_ARMED,
    RULE_STATE_CANCELLED,
    RULE_STATE_ERROR,
    RULE_STATE_EXPIRED,
    SWEEP_INTERVAL_SECONDS,
)
from .rules import (
    RuleError,
    RuleRecord,
    build_rule,
    is_finished,
    mark_action_ran,
    mark_ended,
    mark_fired,
    now_seconds,
    rearmable,
)

if TYPE_CHECKING:
    from .runtime import HasspyRuntime

_LOGGER = logging.getLogger(__name__)

RULE_DOMAIN = "hasspy_rule"

# Longest a single rule may stay armed, whatever the caller asks for. A typo
# must not be able to sit in the store for weeks.
MAX_RULE_EXPIRY_SECONDS = 30 * 24 * 3600.0


def state_snapshot(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Every live state, in the plain-dict shape the pure checks expect."""
    return {
        state.entity_id: {
            "state": state.state,
            "attributes": dict(state.attributes),
        }
        for state in hass.states.async_all()
    }


class RuleEngine:
    """Owns every armed rule: attach, fire, expire, cancel, retire."""

    def __init__(self, hass: HomeAssistant, runtime: HasspyRuntime) -> None:
        self.hass = hass
        self.runtime = runtime
        # Detach handles for armed rules, keyed by rule_id.
        self._detach: dict[str, CALLBACK_TYPE] = {}
        self._cancel_sweep: CALLBACK_TYPE | None = None
        # Arm tasks in flight. They are tracked so a stop can wait for them: an
        # arm that completes after teardown would otherwise register a trigger
        # on a discarded engine.
        self._arm_tasks: set[asyncio.Task] = set()
        self._stopped = False

    # -- lifecycle -----------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Re-arm every stored rule that is still armed and still in the future.

        Called from `HasspyRuntime.async_start`, so a HA restart does not
        silently disarm rules the user armed yesterday.
        """
        for record in list(self.runtime.store.rules.values()):
            if rearmable(record):
                self._spawn_arm(record)
            elif record.state == RULE_STATE_ARMED:
                # A point-in-time rule whose moment passed while we were down.
                mark_ended(record, RULE_STATE_EXPIRED)
                self.runtime.store.async_schedule_save()
        if self._cancel_sweep is None:
            self._cancel_sweep = async_track_time_interval(
                self.hass,
                self._async_sweep_tick,
                timedelta(seconds=SWEEP_INTERVAL_SECONDS),
            )
        self.runtime.async_sync_rules_all()

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        for task in list(self._arm_tasks):
            task.cancel()
        self._arm_tasks.clear()
        for detach in self._detach.values():
            detach()
        self._detach.clear()
        if self._cancel_sweep is not None:
            self._cancel_sweep()
            self._cancel_sweep = None

    def _spawn_arm(self, record: RuleRecord) -> None:
        """Run `_async_arm` as a tracked task."""
        task = self.hass.async_create_task(self._async_arm(record))
        if task is None:  # pragma: no cover - only when hass is shutting down
            return
        self._arm_tasks.add(task)
        task.add_done_callback(self._arm_tasks.discard)

    # -- public API ----------------------------------------------------------

    async def async_add(
        self,
        *,
        name: str,
        action: dict[str, Any],
        trigger: dict[str, Any] | None = None,
        fire_at: float | None = None,
        bridge: str | None = None,
        key: str | None = None,
        allow_any_action: bool = False,
        fire_now_if_true: bool = False,
        expires_in: float | None = DEFAULT_RULE_EXPIRY_SECONDS,
    ) -> RuleRecord:
        """Validate, persist and arm a new rule. Raises `RuleError`."""
        if trigger is not None:
            trigger = await self._async_validate_trigger(trigger)

        if expires_in is not None and expires_in > MAX_RULE_EXPIRY_SECONDS:
            expires_in = MAX_RULE_EXPIRY_SECONDS

        record, already_true = build_rule(
            name=name,
            action=action,
            trigger=trigger,
            fire_at=fire_at,
            states=state_snapshot(self.hass),
            bridge=bridge,
            key=key,
            allow_any_action=allow_any_action,
            fire_now_if_true=fire_now_if_true,
            expires_in=expires_in,
        )
        if bridge:
            self.runtime.store.ensure_bridge(bridge)

        self.runtime.store.put_rule(record)
        self.runtime.store.async_schedule_save()
        await self.runtime.async_upsert_rule(record)

        if already_true:
            # The user asked for it explicitly: act now rather than wait for a
            # transition that will never come.
            await self._async_fire(record, reason="condition already true")
        else:
            await self._async_arm(record)

        self.runtime.async_sync_rule(record.rule_id)
        _LOGGER.info(
            "hasspy rule %s armed (%s -> %s)",
            record.rule_id,
            record.name,
            record.action,
        )
        return record

    async def async_cancel(self, rule_id: str, *, reason: str = "cancelled") -> bool:
        """Disarm and retire a rule. Idempotent; False if it was unknown."""
        record = self.runtime.store.get_rule(rule_id)
        if record is None:
            return False
        self._detach_rule(rule_id)
        if record.is_active:
            # A user-initiated cancel is not an error: keep it out of
            # `last_error`, which the entity and the payload surface as a fault.
            record.cancel_reason = reason
            mark_ended(record, RULE_STATE_CANCELLED)
        self.runtime.store.async_schedule_save()
        self.runtime.async_sync_rule(rule_id)
        return True

    async def async_forget(self, rule_id: str) -> bool:
        """Remove a finished rule and its entities entirely."""
        record = self.runtime.store.get_rule(rule_id)
        if record is None:
            return False
        self._detach_rule(rule_id)
        await self.runtime.async_remove_rule(rule_id)
        self.runtime.store.drop_rule(rule_id)
        self.runtime.store.async_schedule_save()
        return True

    def describe(self, rule_id: str) -> dict[str, Any] | None:
        record = self.runtime.store.get_rule(rule_id)
        return self.runtime.rule_payload(record) if record else None

    # -- arming --------------------------------------------------------------

    async def _async_validate_trigger(
        self, trigger: dict[str, Any]
    ) -> dict[str, Any]:
        """Run HA's own validation so a bad trigger fails here, not at fire time."""
        try:
            validated = await async_validate_trigger_config(self.hass, [trigger])
        except vol.Invalid as err:
            raise RuleError(f"invalid trigger: {err}") from err
        except HomeAssistantError as err:
            raise RuleError(f"invalid trigger: {err}") from err
        return validated[0]

    async def _async_arm(self, record: RuleRecord) -> None:
        """Attach the trigger (or the point-in-time timer) plus the expiry timer."""
        if not record.is_active:
            return
        self._detach_rule(record.rule_id)

        detachers: list[CALLBACK_TYPE] = []

        if record.fire_at is not None:
            when = dt_util.utc_from_timestamp(record.fire_at)
            if when <= dt_util.utcnow():
                mark_ended(record, RULE_STATE_EXPIRED)
                self.runtime.store.async_schedule_save()
                self.runtime.async_sync_rule(record.rule_id)
                return
            detachers.append(
                async_track_point_in_time(
                    self.hass,
                    partial(self._async_fire_timer, record),
                    when,
                )
            )
        elif record.trigger is not None:
            detach = await async_initialize_triggers(
                self.hass,
                [record.trigger],
                self._action_callback(record),
                RULE_DOMAIN,
                record.name,
                self._log_trigger,
            )
            if detach is None:
                mark_ended(
                    record,
                    RULE_STATE_ERROR,
                    error="the trigger could not be attached",
                )
                self.runtime.store.async_schedule_save()
                self.runtime.async_sync_rule(record.rule_id)
                return
            detachers.append(detach)

        if record.expires_at is not None:
            detachers.append(
                async_track_point_in_time(
                    self.hass,
                    partial(self._async_expire_timer, record),
                    dt_util.utc_from_timestamp(record.expires_at),
                )
            )

        def detach_all() -> None:
            for detach in detachers:
                detach()

        # The rule may have been cancelled, fired or expired while the trigger
        # was being attached (attach involves an await). Re-checking here keeps
        # a stale arm from leaving a live trigger nothing owns - which after an
        # unload would fire into a discarded engine.
        if not record.is_active or self._stopped:
            detach_all()
            return

        self._detach[record.rule_id] = detach_all
        self.runtime.async_sync_rule(record.rule_id)

    def _detach_rule(self, rule_id: str) -> None:
        detach = self._detach.pop(rule_id, None)
        if detach is not None:
            detach()

    # Timer callbacks. They are coroutine functions on purpose: Home Assistant
    # treats a plain sync callable as an executor job, so a lambda calling
    # `hass.async_create_task` would run in a worker thread and be reported as
    # a thread-safety violation.
    async def _async_fire_timer(self, record: RuleRecord, _now: Any = None) -> None:
        await self._async_fire(record, reason="scheduled time reached")

    async def _async_expire_timer(self, record: RuleRecord, _now: Any = None) -> None:
        await self._async_expire(record)

    async def _async_sweep_tick(self, _now: Any = None) -> None:
        await self.async_sweep()

    def _action_callback(self, record: RuleRecord):
        """The callable HA's trigger machinery invokes when the trigger fires."""

        async def on_trigger(
            run_variables: dict[str, Any], context: Context | None = None
        ) -> None:
            await self._async_fire(record, reason="trigger fired", context=context)

        return on_trigger

    @staticmethod
    def _log_trigger(level: int, message: str) -> None:
        _LOGGER.log(level, "hasspy rule: %s", message)

    # -- firing --------------------------------------------------------------

    async def _async_fire(
        self,
        record: RuleRecord,
        *,
        reason: str,
        context: Context | None = None,
    ) -> None:
        """Run the action once, then retire the rule.

        A rule is one-shot: the trigger is detached *before* the action runs, so
        a slow or failing action cannot cause a second firing.
        """
        if not record.is_active:
            return
        self._detach_rule(record.rule_id)
        mark_fired(record)
        self.runtime.store.async_schedule_save()

        script = None
        try:
            sequence = cv.SCRIPT_SCHEMA(list(record.script_sequence))
            script = Script(
                self.hass,
                sequence,
                f"hasspy rule {record.rule_id}",
                RULE_DOMAIN,
            )
            await script.async_run({}, context=context)
            mark_action_ran(record)
        except Exception as err:  # - a failed action must not kill the engine
            _LOGGER.exception("hasspy rule %s action failed", record.rule_id)
            mark_ended(record, RULE_STATE_ERROR, error=f"{type(err).__name__}: {err}")
        finally:
            # A top-level `Script` registers itself in hass.data[DATA_SCRIPTS] and
            # is only removed by async_unload(). Without this, every firing leaks
            # one script instance for the lifetime of Home Assistant.
            if script is not None:
                await script.async_unload()

        self.runtime.async_sync_rule(record.rule_id)
        self._log_rule(record, f"fired ({reason})")

    async def _async_expire(self, record: RuleRecord) -> None:
        if not record.is_active:
            return
        self._detach_rule(record.rule_id)
        mark_ended(record, RULE_STATE_EXPIRED)
        self.runtime.store.async_schedule_save()
        self.runtime.async_sync_rule(record.rule_id)
        self._log_rule(record, "expired without firing")

    def _log_rule(self, record: RuleRecord, message: str) -> None:
        """Best-effort log line, so a firing rule is visible in history.

        Only forwarded to ``hasspy.log`` when the rule has a bridge: that
        service self-heals an unknown bridge by *creating* it, so passing a
        made-up name here registered a phantom, permanently-offline bridge
        device. A rule created by hand or by voice has no bridge, so its line
        stays in the HA log instead.
        """
        text = f"rule {record.rule_id} ({record.name}) {message}"
        if not record.bridge:
            _LOGGER.info("hasspy: %s", text)
            return
        self.hass.async_create_task(
            self.hass.services.async_call(
                "hasspy",
                "log",
                {
                    "bridge": record.bridge,
                    "automation": record.key,
                    "message": text,
                    "level": "info" if record.state != RULE_STATE_ERROR else "error",
                },
                blocking=False,
            )
        )

    # -- housekeeping --------------------------------------------------------

    async def async_sweep(self) -> int:
        """Drop finished rules after their retention window. Returns a count.

        Keeps "the last outcome is still visible" without letting the store
        grow without bound.
        """
        cutoff = now_seconds() - RULE_RETENTION_SECONDS
        removed = 0
        for record in list(self.runtime.store.rules.values()):
            if not is_finished(record):
                continue
            # Measure from when it finished, falling back to creation for a
            # record written before `finished_at` existed.
            ended_at = record.finished_at or record.fired_at or record.created_at
            if ended_at > 0 and ended_at >= cutoff:
                continue
            await self.async_forget(record.rule_id)
            removed += 1
        return removed
