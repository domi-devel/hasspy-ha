# hasspy — Home Assistant companion integration

The Home-Assistant-side half of [hasspy](https://github.com/domi-devel/hasspy),
a standalone Python automation runtime.

hasspy apps already publish sensor values by POSTing to `/api/states/<id>`, but
those "virtual" entities have **no entity-registry entry**, disappear on every
HA restart until the app re-runs, and cannot carry a `state_class`. This
integration replaces that with a real, registry-backed entity platform, and adds
a lifecycle manager so the throw-away entities a *debugging* run creates do not
pile up — while a crashed automation stays inspectable for hours.

## Install (HACS)

1. HACS → Integrations → ⋮ → *Custom repositories* → add this repo, category
   *Integration*.
2. Install **hasspy**, restart Home Assistant.
3. Settings → Devices & Services → *Add integration* → **hasspy**.

No configuration is needed; the integration is a local sink that hasspy
publishes into.

## Services

| Service | Purpose |
| --- | --- |
| `hasspy.register_bridge` | Announce a hasspy process (idempotent). |
| `hasspy.heartbeat` | Keep a bridge alive; refresh its automation list. |
| `hasspy.log` | Record a log line from an automation (logbook + `sensor.<bridge>_last_log`). |
| `hasspy.create_entity` | Create/update a dynamic entity; **returns `entity_id`**. |
| `hasspy.set_entity_state` | Push a value, refreshing the entity's lease. |
| `hasspy.delete_entity` | Remove a dynamic entity. |
| `hasspy.list_bridges` / `list_entities` | Introspect what hasspy has published. |
| `hasspy.release_session` | Delete a debug session immediately (graceful exit). |
| `hasspy.sweep` | Run GC now (`dry_run` shows verdicts). |
| `hasspy.pin` / `unpin` | Exempt debug entities from GC. |

Entities are grouped per bridge under a device `hasspy <bridge>`, together with
three per-bridge entities: `binary_sensor.<bridge>_bridge_online`,
`sensor.<bridge>_automations`, and `sensor.<bridge>_last_log` (the most recent
line an automation logged; see [`docs/LOGGING.md`](docs/LOGGING.md)).

### Values and controls

`create_entity` takes two kinds of entity, and the difference matters:

* **Values** (`sensor`, `binary_sensor`) — hasspy is the writer; a debug run's
  values are lease-managed.
* **Controls** (`number`, `select`, `switch`, `datetime`) — *settings* the user
  edits in Home Assistant. They are permanent (never garbage-collected) and
  carry a `control_value` attribute so hasspy can read any domain uniformly.
  This is what replaces scattered `input_number` / `input_select` /
  `input_boolean` / `input_datetime` helpers.

See [`docs/CONTROLS.md`](docs/CONTROLS.md).

## The idea in one picture

A production bridge's entities live forever. A **debug** bridge's entities carry
a *lease*: as long as the owner keeps touching them they are `active`; once the
lease lapses they turn `unavailable` (still visible!) for a grace window; only
then are they collected. Crucially, **all aging is frozen while the owning
hasspy process is unreachable**, so a crash of several hours is never mistaken
for "cleanup due".

See [`docs/DEBUG_ENTITIES.md`](docs/DEBUG_ENTITIES.md) for the full concept and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for how the pieces fit together.

## Development

The integration test suite runs against a real Home Assistant instance via
`pytest-homeassistant-custom-component`, which pins an exact HA version and
requires Python ≥ 3.14 for recent HA releases.

```bash
uv venv --python 3.14 .venv-ha
uv pip install --python .venv-ha/bin/python -r requirements_test.txt
.venv-ha/bin/python -m pytest
```

The pure lifecycle tests (`tests/test_debug_lifecycle.py`) need no Home
Assistant and skip the integration tests when it is absent (`pytest` with the
system interpreter still gives useful signal).

Verified against HA **2026.9.3** (the deployment generation) and **2025.1.4**
(oldest supported).

## License

MIT — see [LICENSE](LICENSE).
