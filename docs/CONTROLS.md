# Controls (settings) vs values

hasspy's dynamic entities come in two flavours, and the distinction is load
bearing:

| | **Value** | **Control** |
|---|---|---|
| Domains | `sensor`, `binary_sensor` | `number`, `select`, `switch`, `datetime` |
| Who writes | hasspy | the **user** (in the UI), or hasspy via the native service |
| Source of truth | hasspy | Home Assistant |
| Lifetime | lease-managed when `scope: debug` | permanent (a setting is never garbage) |
| Replaces | REST `set_state` virtual sensors | `input_number` / `input_select` / `input_boolean` / `input_datetime` helpers |

A **value** is data hasspy measured. A **control** is a setting the user owns.
Collapsing them is what makes "just use `input_number`" tempting — and also what
scatters settings across the helper UI.

## What hasspy reads

Controls carry a `control_value` attribute that normalises the four domains, so
hasspy does not have to parse `"50.0"` vs `"on"` vs `"2026-10-07..."`:

```python
offset = self.get_control_float("number.shutter_sunrise_offset", default=50)
enabled = self.get_control_bool("switch.shading_enabled", default=True)
mode = self.get_control("select.ad_skip_mode", default="mixed")
```

`get_control` returns `default` when the entity is missing, `unknown` or
`unavailable`, so an app whose controls have not been created yet still runs.

## Creating them

Apps create their own controls on startup — idempotently — so the entity lives
with the code that uses it instead of being a hand-made helper:

```python
class Roller(Automation):
    def initialize(self):
        self.sunrise_offset_id = self.create_control(
            "sunrise_offset",
            name="Shutter sunrise offset",
            object_id="shutter_sunrise_offset",   # stable public entity_id
            min=-300, max=300, step=1, unit="min", mode="box",
            value=50,                              # only used on first create
        )
        # A control is an ordinary entity: react to edits with a trigger.
        self.subscribe_state_trigger(self.on_change, entity_id=self.sunrise_offset_id)
```

`object_id` is what keeps `number.shutter_sunrise_offset` stable and predictable
in dashboards — it is **not** derived from the (renamable) device name.

### Shared controls

Pass `shared=True` to create a bridge-level control not owned by one automation,
so two app instances ensure the *same* entity rather than two copies:

```python
self.sunset_offset_id = self.create_control(
    "sunset_offset", object_id="shutter_sunset_offset", shared=True, value=-19, ...
)
```

An existing control's value is **preserved** on re-create unless `value` is
passed — and even then only on the *first* create. That matters because
`create_control` is normally called in `initialize()` with its default value on
every startup: the default seeds the entity once and never overrides the user
afterwards.

### Publishing values

A value entity (`sensor`/`binary_sensor`) is written by hasspy, so use
`publish(key, state, ...)` — create-or-update and return the `entity_id`. Call
it once in `initialize()` to create the entity, then again wherever the value
changes:

```python
class Roller(Automation):
    def initialize(self):
        self.publish("up", domain="sensor", object_id="roller_normal_up",
                     name="Roller normal up", device_class="timestamp")
        ...
    def on_sunrise_change(self, ...):
        self.publish("up", next_sunrise.isoformat())
```

Unlike the old REST `set_state`, the entity is **registered**: it keeps a stable
`entity_id` and survives a Home Assistant restart instead of vanishing until the
next write. `scope="debug"` leases a throw-away value (see DEBUG_ENTITIES.md).

Pass descriptors only on the create call. A state-only `publish` leaves them
untouched, so `device_class`/unit/name are never wiped by a value update.

## Writing them

Most apps only read controls. To write one:

```python
self.set_control("number.hasspy_setpoint", -1.5)
```

which uses the domain's native service (`number.set_value`, `select.select_option`,
`switch.turn_on/off`, `datetime.set_value`) — no hasspy-specific write path.

## Watching for changes

Every control change fires a `hasspy_control_changed` event with
`bridge`, `automation`, `key`, `entity_id` and `value`. Prefer the per-entity
state trigger (as the roller app does) unless you need to react to many controls
at once:

```yaml
trigger:
  - platform: event
    event_type: hasspy_control_changed
```

## Migrating an `input_*` helper

1. Confirm nothing else uses it: search `automations.yaml`, `scripts.yaml`,
   `templates.yaml`, `.storage/lovelace.*` and config entries. If a dashboard
   uses it, keep the id stable via `object_id`.
2. Create the control from the app with `create_control(...)`.
3. Replace `get_state("input_number.x")["state"]` with
   `get_control_float("number.x", default=...)`.
4. Deploy, verify the entity exists and the value carried over.
5. Delete the old helper (Settings → Devices & Services → Helpers).

Controls are never garbage-collected, including on a `scope: debug` bridge —
a setting is not a thing to tidy away.
