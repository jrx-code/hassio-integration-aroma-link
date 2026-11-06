"""Entity ids and phase-aware counters; pure, no Home Assistant import.

Entity ids follow the cloud integration (cjam28/homeassistant_aroma-link): an English suffix per
entity, independent of the UI language, so automations and cards written for the cloud
integration keep their suffixes. The prefix is ``aroma_link_<mac with underscores>``, which is what
the device's default name produced in every install so far.
"""
from __future__ import annotations

import re

# unique_id key -> entity id suffix; keys absent here use the key itself.
# The cloud integration names the refill button entity "Refill" (``button.<device>_refill``).
ENTITY_ID_SUFFIX = {
    "oil_refill_keep_calibration": "refill",
}

_UNIQUE_ID = re.compile(r"^aroma_([0-9a-f]{12})_(.+)$")


def mac_slug(mac_hex: str) -> str:
    """``aabbccddeeff`` or ``AA:BB:CC:DD:EE:FF`` -> ``aa_bb_cc_dd_ee_ff``."""
    h = mac_hex.replace(":", "").lower()
    return "_".join(h[i:i + 2] for i in range(0, 12, 2))


def object_id(mac: str, key: str) -> str:
    return f"aroma_link_{mac_slug(mac)}_{ENTITY_ID_SUFFIX.get(key, key)}"


def wanted_entity_id(domain: str, unique_id: str) -> str | None:
    """The entity id an entity with this unique_id should have, or None for a foreign id."""
    m = _UNIQUE_ID.match(unique_id or "")
    if not m:
        return None
    return f"{domain}.{object_id(m.group(1), m.group(2))}"


def is_generated(entity_id: str, domain: str, unique_id: str) -> bool:
    """True when the id still carries the generated device prefix (not a user's own rename)."""
    m = _UNIQUE_ID.match(unique_id or "")
    return bool(m) and entity_id.startswith(f"{domain}.aroma_link_{mac_slug(m.group(1))}_")


# Each counter means something only in its own phase; outside it the device reports a frozen
# value from the last run (with power off it keeps the old slot's 60/300 after a 120/120 write).
_PHASE_OF = {"work_remain": "diffusing", "pause_remain": "paused"}


def phase_remaining(status: dict | None, key: str) -> int | None:
    if not status:
        return None
    if not status.get("power") or status.get("work_status") != _PHASE_OF[key]:
        return 0
    return status.get(key)
