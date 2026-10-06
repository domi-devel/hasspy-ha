"""Constants for the hasspy companion integration.

hasspy is a standalone Python automation runtime (see the hasspy-config repo).
This integration does not *contain* hasspy; it gives hasspy a registry-backed
entity platform to publish into, and a lifecycle manager for the throw-away
entities a debugging run creates.
"""

from __future__ import annotations

DOMAIN = "hasspy"
NAME = "hasspy"

# Keep in sync with manifest.json (used for the device registry sw_version).
MANIFEST_VERSION = "0.1.0"

# --- bridge-scoped services (names in <domain>.<service>) -------------------
SERVICE_REGISTER_BRIDGE = "register_bridge"
SERVICE_REMOVE_BRIDGE = "remove_bridge"
SERVICE_HEARTBEAT = "heartbeat"
SERVICE_LOG = "log"

SERVICE_CREATE_ENTITY = "create_entity"
SERVICE_SET_ENTITY_STATE = "set_entity_state"
SERVICE_DELETE_ENTITY = "delete_entity"

SERVICE_LIST_BRIDGES = "list_bridges"
SERVICE_LIST_ENTITIES = "list_entities"

# --- debug lifecycle --------------------------------------------------------
SERVICE_RELEASE_SESSION = "release_session"
SERVICE_SWEEP = "sweep"
SERVICE_PIN = "pin"
SERVICE_UNPIN = "unpin"

# --- scopes -----------------------------------------------------------------
SCOPE_PRODUCTION = "production"
SCOPE_DEBUG = "debug"
SCOPES = (SCOPE_PRODUCTION, SCOPE_DEBUG)

# --- dynamic entity domains -------------------------------------------------
# Value entities: hasspy pushes state, Home Assistant reads it.
VALUE_DOMAINS = ("sensor", "binary_sensor")
# Control entities: the value is a *setting*. Home Assistant is the source of
# truth (the user edits it in the UI), and hasspy both reads and can write it.
CONTROL_DOMAINS = ("number", "select", "switch", "datetime")
DYNAMIC_DOMAINS = VALUE_DOMAINS + CONTROL_DOMAINS

# --- lifecycle states (not HA states; internal bookkeeping) ----------------
STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ORPHANED = "orphaned"

# --- defaults ---------------------------------------------------------------
# Lease TTL: how long a debug entity may go without being (re-)asserted by a
# *live* owner before it is considered stale and rendered `unavailable`.
DEFAULT_LEASE_SECONDS = 60.0
# Grace: how long a stale debug entity is kept around (visible but
# unavailable) while its bridge is still reachable, before it is collected.
# Deliberately long: a crashed automation must remain inspectable for hours.
DEFAULT_GRACE_SECONDS = 6 * 3600.0
# A bridge that has not heartbeated for this long counts as "unreachable", and
# all aging of its entities is frozen until it comes back.
DEFAULT_BRIDGE_STALE_SECONDS = 120.0
# How often the garbage collector runs.
SWEEP_INTERVAL_SECONDS = 30.0
# Debounce for writing the store back to disk.
SAVE_DELAY_SECONDS = 2.0

# --- storage ----------------------------------------------------------------
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.dynamic"
