# hasspy-side reporter

**Implemented** in the hasspy library: `hasspy/src/hasspy/reporter.py`, wired
into `hasspy.__main__.start_hasspy`, `hasspy.hassapi.HassApi.add_automation` and
`hasspy.automation.Automation`. It ships through the hasspy image (`build.sh`),
not through HACS.

## What it does

* On startup, checks whether the companion integration is installed (via
  `get_services`, looking for `hasspy.register_bridge`) and, if so, registers
  this process as a **bridge**, then heartbeats it every 30 s.
* Wraps each automation's trigger/event callbacks to record a **last-event
  timestamp** and an **error count**, which the integration surfaces on
  `sensor.<bridge>_automations` and per-automation.
* Registers `atexit` → `release_session`, so a clean exit removes this bridge's
  debug entities immediately.

It is entirely best-effort: a missing integration, a mid-reconnect WebSocket, or
a socket error never raises into automation code.

## Enabling and configuration

Auto-detected by default. Override with environment variables (so `hasspy.yaml`
stays host-agnostic — see `.env.example`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `HASSPY_REPORTER` | auto | `1`/`0` force the reporter on/off |
| `HASSPY_BRIDGE` | `production` | bridge id (use `debug` for a debug run) |
| `HASSPY_SCOPE` | from bridge | `production` or `debug` |
| `HASSPY_DEBUG_TTL` | `60` | lease seconds for debug entities |
| `HASSPY_DEBUG_GRACE` | `21600` | retention after stale, seconds (6 h) |
| `HASSPY_HEARTBEAT` | `30` | heartbeat interval, seconds |

A debug run is just `HASSPY_BRIDGE=debug` (scope is derived as `debug`).

## Publishing entities from an app (implemented helper)

`Automation` now exposes two thin wrappers, so apps do not hand-roll service data:

```python
class Roller(Automation):
    def on_sunset_change(self, *args, **kwargs):
        entity_id = self.create_entity(
            "down",
            device_class="timestamp",
            name="Roller down",
            state=next_sunset.isoformat(),
        )
        ...
        self.set_entity_state("down", next_sunset.isoformat())
```

Both require the reporter (i.e. the integration). Without it they log once and
return `None`; apps can keep their existing REST `set_state` path as a fallback.

## Known limitation

`create_entity` uses `call_service(..., return_response=True)` to return the
`entity_id`. hasspy's `send_message(return_result=True)` busy-waits on the
calling thread, and app callbacks run on the event loop, so this briefly blocks
the loop for one round-trip (a few ms). Acceptable at current volumes; a future
improvement is an async result path.

## Original design reference

The rest of this file is the design that was implemented, kept for context.

### Bridge presence + per-automation telemetry

Only hasspy knows which apps it loaded and when each one last ran a callback.


## 1. `hasspy/reporter.py`

```python
"""Bridge presence + per-automation telemetry for the hasspy HA integration."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT = 30.0  # seconds


class BridgeReporter:
    """Publishes this process as a "bridge" to the hasspy integration.

    Every call is best-effort: if the integration is not installed, or HA is
    reconnecting, we log at debug and carry on. Telemetry must never be able to
    take down an automation.
    """

    def __init__(self, conn, bridge, *, name=None, scope="production",
                 heartbeat=DEFAULT_HEARTBEAT, ttl=60.0, grace=6 * 3600.0):
        self.conn = conn
        self.bridge = bridge
        self.name = name or f"hasspy ({bridge})"
        self.scope = scope
        self.heartbeat = heartbeat
        self.ttl = ttl
        self.grace = grace
        self.run_id = uuid.uuid4().hex
        self._started = time.time()
        self._automations: dict[str, dict] = {}

    # -- telemetry -----------------------------------------------------------

    def note_automation(self, automation_id, cls, *, loaded=True):
        entry = self._automations.setdefault(
            automation_id, {"id": automation_id, "cls": cls, "loaded": loaded,
                            "last_event": None, "errors": 0}
        )
        entry["loaded"] = loaded
        entry["cls"] = cls

    def note_event(self, automation_id):
        entry = self._automations.get(automation_id)
        if entry is not None:
            entry["last_event"] = time.time()

    def note_error(self, automation_id):
        entry = self._automations.get(automation_id)
        if entry is not None:
            entry["errors"] = entry.get("errors", 0) + 1

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        self._call("register_bridge", {
            "bridge": self.bridge, "name": self.name, "scope": self.scope,
            "run_id": self.run_id, "ttl": self.ttl, "grace": self.grace,
            "automations": list(self._automations.values()),
        })

    def beat(self):
        self._call("heartbeat", {
            "bridge": self.bridge, "run_id": self.run_id,
            "automations": list(self._automations.values()),
        })

    def release(self):
        """Graceful shutdown: drop every debug entity this run created."""
        self._call("release_session", {"bridge": self.bridge, "run_id": self.run_id})

    # -- plumbing -----------------------------------------------------------

    def _call(self, service, data):
        try:
            self.conn.send_message({
                "type": "call_service", "domain": "hasspy",
                "service": service, "service_data": data,
            })
        except Exception as e:  # noqa: BLE001 - telemetry is never fatal
            logger.debug("hasspy reporter: %s failed: %r", service, e)
```

## 2. Wiring in `automation.py`

`_timed_run` already wraps every scheduled callback and `ConnectionManager`
dispatches every trigger callback through one place, so both the automation
count and last-event/error counters come from two small hooks:

```python
# Automation.__init__ / initialize()
self.reporter = getattr(self, "reporter", None)   # injected by HassApi

# around a user callback
def _instrument(self, automation_id, func, *args):
    try:
        result = func(*args)
    except Exception:
        if self.reporter:
            self.reporter.note_error(automation_id)
        raise
    if self.reporter:
        self.reporter.note_event(automation_id)
    return result
```

## 3. Wiring in `__main__.start_hasspy`

```python
reporter = BridgeReporter(
    api.conn,
    bridge=os.environ.get("HASSPY_BRIDGE", "production"),
    scope=os.environ.get("HASSPY_SCOPE", "production"),
)
for k, v in config.automations.dict().items():
    ...
    for i, arg in enumerate(v):
        automation = maps[k]["automation_class"](arg)
        automation.reporter = reporter
        reporter.note_automation(f"{k}#{i}", maps[k]["automation_name"])
        api.add_automation(automation)
reporter.start()
api.conn.add_heartbeat(reporter.beat)   # every `reporter.heartbeat` seconds
atexit.register(reporter.release)       # or in the shutdown path
```

A debugging run then differs only by env: `HASSPY_BRIDGE=debug HASSPY_SCOPE=debug`.

## 4. Entity creation helper

A thin, optional convenience so apps do not hand-roll the JSON:

```python
def publish_sensor(self, key, state, *, unit=None, device_class=None,
                   state_class=None, attributes=None, automation=None):
    return self.call_service(
        "hasspy.create_entity",
        data={
            "bridge": self.reporter.bridge,
            "automation": automation or self.automation_id,
            "key": key, "domain": "sensor", "state": state,
            "unit": unit, "device_class": device_class,
            "state_class": state_class, "attributes": attributes or {},
        },
        return_response=True,
    )["entity_id"]
```
