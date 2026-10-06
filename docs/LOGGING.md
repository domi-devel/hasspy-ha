# Logging from automations

When an automation runs on a quiet day nothing much happens, and when it goes
wonky you are usually looking at it after the fact. `Automation.log()` puts a
line where you can actually see it.

```python
self.log("mowing plan: no zone due")                       # routine heartbeat
self.log(f"kept pump on: run {minutes:.0f} min",           # a decision
         level="warning")
self.log(f"sensor {entity} unreadable, skipping cycle",    # a failure
         level="error")
```

The line lands in three places at once:

| Where | How to see it |
| --- | --- |
| The HA logbook timeline | Logbook card, or History → Logbook |
| A per-bridge tail | `sensor.<bridge>_last_log` (state = newest line; `log` attribute = the last 100) |
| The hasspy process log | `docker logs hasspy`, at the matching level |

`logbook=False` suppresses the logbook write (useful for high-rate debug lines).
`level` is `debug` / `info` / `warning` / `error` (default `info`).

It never raises — logging a failure must not *be* the failure — and it works
before the integration connects: the line still lands in the hasspy log.

## Reading it from Home Assistant

`sensor.<bridge>_last_log` is a normal sensor, so it works on any dashboard:

- **A markdown card** with `{{ state_attr('sensor.production_last_log', 'log') }}`
  to show the tail.
- **A Logbook card** filtered to the hasspy device, to see the timeline.
- In an automation: `trigger: state` on `sensor.production_last_log`, or a
  template checking `state_attr('sensor.production_last_log', 'log')[0]['level'] == 'error'`.

The attribute is a list of
`{"time": <epoch>, "automation": "pumpwatchdog#0", "level": "error", "message": "..."}`
newest-first.

## What to log

Log things a human would want to know at 3am, not every state change:

- **Decisions** with their reason: `"kept pump on: run 42 min"`,
  `"beschatte cover.rollladen_marie: 100% -> 35%"`.
- **Failures and recoveries**: `"3 Kurzlaeufe in 15 min - Verdacht auf undichte
  Leitung, Pumpe abgeschaltet"` (level `error`),
  `"Pumpe laeuft nach dem Stromzyklus wieder"` (level `warning`).
- **A low-rate heartbeat** (`level="debug"`) so an idle automation still proves
  it is alive — e.g. the mowing plan logs `"keine Zone faellig"` when nothing is
  due.

Do **not** log on every sensor reading; the ring buffer is 100 lines and the
logbook is a user-facing timeline.

## How it is wired

```
Automation.log()  ->  BridgeReporter.log()  ->  hasspy.log service
                                                   |-> bridge record ring buffer (100)
                                                   |-> HA system log (matching level)
                                                   `-> logbook.log service (if available)
```

The ring buffer is persisted with the rest of the bridge record, so the tail
survives a Home Assistant restart.
