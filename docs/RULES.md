# One-shot rules

A **rule** is a trigger plus an action, armed on demand and run **once**:

> "Set the storage override to *Kein Override* when the SoC reaches 60 %."

Rules exist so a one-off intention does not require editing an automation's
YAML. They are created from a service call (so an automation, a dashboard or a
voice assistant can create them), appear as ordinary registry-backed entities,
and can be cancelled with one tap.

## Why not just make an automation?

An automation is permanent: its triggers are attached forever until someone
disables it. A rule is the opposite — armed once, fires once, then finished and
retired. Creating, listing and cancelling rules needs no config editing, which
is what makes the voice path possible.

## What a rule looks like in Home Assistant

Every rule creates two entities on a dedicated **`hasspy rules`** device (not on
the bridge device, so they do not clutter the automation list):

| Entity | Purpose |
| --- | --- |
| `sensor.hasspy_rule_<rule_id>` | State: `armed`, `fired`, `cancelled`, `expired`, `error`. Attributes carry the trigger, the action, human-readable descriptions, `armed`, `fired_at`, `expires_at`, `warnings`, `last_error` and `cancel_entity_id`. |
| `button.hasspy_rule_<rule_id>_cancel` | Cancels the rule. Unavailable once the rule is finished. |

Both are removable and re-creatable: they are ordinary HA entities, so a
dashboard can show them, an automation can watch them, and a voice assistant can
read them.

## Services

### `hasspy.add_rule`

| Field | Meaning |
| --- | --- |
| `name` | Required. Shown as the sensor's name and used in logs. |
| `trigger` | A normal HA trigger config — any platform. Example: `{platform: numeric_state, entity_id: sensor.soc, above: 60}`. |
| `at` / `in` | Point-in-time alternative to `trigger`: `at` takes ISO-8601 or `HH:MM` (next occurrence), `in` takes seconds or `5m`/`2h`/`1d`. |
| `action` | Required. Either `{entity_id, value}` to write a control, or a service call with `allow_any_action`. |
| `allow_any_action` | Permit an arbitrary service call as the action (off by default). |
| `fire_now_if_true` | Run the action immediately when the condition already holds (see below). |
| `expires_in` | Seconds before the rule is disarmed and marked expired. Default **24 h**, capped at 30 days. For `at`/`in` rules the expiry is always placed *after* the scheduled moment, so a rule two days out is not expired before it can fire. |
| `bridge` / `key` | Optional. Attribution and grouping; a `bridge` is auto-registered. |

Returns the rule payload (see `list_rules`), including `rule_id`, `entity_id` and
`cancel_entity_id`.

### `hasspy.cancel_rule`

`rule_id` disarms it and marks it `cancelled` (`reason` is stored in
`last_error`). `forget: true` deletes the rule and both entities.

### `hasspy.list_rules`

Returns every rule newest-first, optionally filtered by `bridge`, `active_only`
or `include_finished`.

## Plausibility: what is checked before a rule is armed

Rejections come back as a service validation error with an actionable message.
This matters because an armed rule that can never fire is worse than an error —
it looks like it is working.

**Trigger**

* the trigger must have a `platform`, and every referenced entity must exist;
* `numeric_state` needs at least one of `above`/`below` — either a number or the
  entity_id of a helper Home Assistant reads at fire time;
* `above < below`;
* numeric thresholds must be inside the entity's plausible range, using:
  * the control's declared `min`/`max` (number entities expose these), or
  * a hard `0..100` bound when `unit_of_measurement` is `%`.
    This is what rejects *"when the SoC reaches 1500 %"*;
* `state` triggers reject a `to` value the target `select` cannot take;
* `time` triggers need `at`.

**Action**

* `{entity_id, value}` must target a writable control (`select`, `number`,
  `switch`, `datetime`);
* `select` values must be one of the entity's `options`;
* `number` values must be numeric and inside `min`..`max`;
* `switch` values must be booleans; `datetime` values must be ISO strings;
* anything else needs `allow_any_action: true`.

A missing target entity is a **warning**, not a rejection: the entity may be
created later by a hasspy app that is still starting.

## The gotcha that shapes the API

Home Assistant triggers fire on a **transition**, never on attach. If the SoC is
already 70 % and a rule watches `above: 60`, that rule can never fire — HA only
reacts to an entity *changing into* the range.

So:

* `add_rule` **rejects** a rule whose condition is already satisfied, with an
  error explaining it (`trigger_already_satisfied`), and
* `fire_now_if_true: true` means "I know — act now", which runs the action
  immediately and records the rule as `fired`.

Related, inherited limitations:

* a pending `for:` duration is an in-memory timer, so an in-flight duration
  restarts after a restart (the rule itself is re-armed);
* an entity already inside the target range stays silent until it leaves and
  re-enters.

## Restart behaviour

Rules are stored in the same durable store as entities
(`.storage/hasspy.dynamic`, `atomic_writes=True`) and are **re-armed** when the
integration loads, so a HA restart or a HACS update does not silently disarm
them. A point-in-time rule whose moment passed while HA was down is marked
`expired` instead of firing late.

Finished rules are kept for 24 h — measured from when the rule *finished* — so
the last outcome stays visible, then swept.

## Logging

A rule's outcome is logged. Rules created with a `bridge` send their line to
`hasspy.log`, so it reaches the bridge's `last_log` sensor and the logbook; a
rule created by hand or by voice has no bridge and its line stays in the Home
Assistant log. (Logging under a made-up bridge name would *create* that bridge,
because `hasspy.log` self-heals unknown bridges.)

## Voice

Assist only offers **scripts** as LLM tools (services are not exposed as tools),
so voice needs a thin script. See `blueprints/` in this repo for
`script.hasspy_add_rule`, which takes `name`, `entity_id`, `value` and an optional
threshold and forwards to `hasspy.add_rule`. Expose it to Assist under
*Settings > Voice assistants > Expose*, then:

> "Set the storage to no override when the battery reaches 60 percent."

## Creating a rule from hasspy

hasspy apps use the plain service call (no library change is required), but the
SDK ships thin wrappers:

```python
entity_id = self.add_rule(
    name="No override at 60% SoC",
    trigger={"platform": "numeric_state",
             "entity_id": "sensor.fronius_soc",
             "above": 60},
    action={"entity_id": "select.speicher_override", "value": "Kein Override"},
)
```

Rules created by an app are **not** re-asserted on every restart (unlike
controls): a rule is a one-off action and must not silently come back after the
user cancelled it. Pass a `key` if you want to find it again with
`list_rules`.

## Limits

* A threshold given as a helper's `entity_id` cannot be range-checked — HA reads
  it at fire time. The rule is accepted with a warning.
* Whether a physical value will *ever* be reached cannot be proven; that is what
  the default 24 h expiry is for.
* `allow_any_action` actions are passed to HA untouched, so their target and
  data are not validated beyond the service name. An action that names a
  missing *entity* is a silent no-op in Home Assistant itself; a missing
  *service* raises and leaves the rule in `error`.
* Only `select` exposes its allowed values to a trigger check, so a `state`
  trigger on another domain can still be armed for a value it can never take.
