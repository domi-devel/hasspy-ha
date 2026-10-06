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
| `hasspy.create_entity` | Create/update a dynamic entity; **returns `entity_id`**. |
| `hasspy.set_entity_state` | Push a state, refreshing the entity's lease. |
| `hasspy.delete_entity` | Remove a dynamic entity. |
| `hasspy.list_bridges` / `list_entities` | Introspect what hasspy has published. |
| `hasspy.release_session` | Delete a debug session immediately (graceful exit). |
| `hasspy.sweep` | Run GC now (`dry_run` shows verdicts). |
| `hasspy.pin` / `unpin` | Exempt debug entities from GC. |

Entities are grouped per bridge under a device `hasspy <bridge>`, together with
two presence entities: `binary_sensor.<bridge>_bridge_online` and
`sensor.<bridge>_automations`.

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

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest
```

## License

MIT — see [LICENSE](LICENSE).
