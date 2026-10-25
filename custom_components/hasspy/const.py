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

# --- one-shot rules ---------------------------------------------------------
SERVICE_ADD_RULE = "add_rule"
SERVICE_CANCEL_RULE = "cancel_rule"
SERVICE_LIST_RULES = "list_rules"

# Rule lifecycle states. A rule is armed once, fires once, then is finished;
# `cancelled`/`expired`/`error` are also terminal.
RULE_STATE_ARMED = "armed"
RULE_STATE_FIRED = "fired"
RULE_STATE_CANCELLED = "cancelled"
RULE_STATE_EXPIRED = "expired"
RULE_STATE_ERROR = "error"
RULE_STATES = (
    RULE_STATE_ARMED,
    RULE_STATE_FIRED,
    RULE_STATE_CANCELLED,
    RULE_STATE_EXPIRED,
    RULE_STATE_ERROR,
)
# A rule that is never reached must not stay armed forever: a typo would
# otherwise sit in the store indefinitely. 24 h covers "tonight"/"tomorrow
# morning" phrasing without leaving surprises days later.
DEFAULT_RULE_EXPIRY_SECONDS = 24 * 3600.0
# Finished rules are kept this long so the last outcome stays visible.
RULE_RETENTION_SECONDS = 24 * 3600.0

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
