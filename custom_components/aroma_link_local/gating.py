"""Pure gating logic (no Home Assistant imports): which power state the schedule wants.

Decision per evaluation, adapted from cjam28/homeassistant_aroma-link v3.1.2 (MIT):

    timed run active           -> ON
    schedule_enabled == False  -> hands off (user owns power)
    inside a normal window     -> HVAC gate AND occupancy gate (each with an off-delay)
    inside a Night Owl period  -> motion seen within the linger window
    otherwise                  -> OFF

Gating modes: ``power`` (the decision drives the power switch) and ``slots`` (default; power
is held ON while the engine owns the device and the decision disarms/re-arms the slots, so the
device never beeps for a gate change).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .model import DeviceModel
from .schedule import active_window, night_owl_period

GATING_POWER = "power"
GATING_SLOTS = "slots"
GATING_MODES = (GATING_POWER, GATING_SLOTS)
HVAC_INACTIVE_ACTIONS = {None, "", "idle", "off"}


@dataclass
class GateConfig:
    climate_entity: str | None = None
    occupancy_entity: str | None = None
    motion_entities: list[str] = field(default_factory=list)
    hvac_on_delay_minutes: int = 1
    hvac_off_delay_minutes: int = 0
    occupancy_off_delay_minutes: int = 2
    gating_mode: str = GATING_SLOTS
    keep_fan_on: bool = True

    @classmethod
    def from_options(cls, options: dict, topic_id: str) -> GateConfig:
        g = (options.get("gates") or {}).get(topic_id) or {}
        return cls(
            climate_entity=g.get("climate_entity") or None,
            occupancy_entity=g.get("occupancy_entity") or None,
            motion_entities=list(g.get("motion_entities") or []),
            hvac_on_delay_minutes=int(g.get("hvac_on_delay_minutes", 1)),
            hvac_off_delay_minutes=int(g.get("hvac_off_delay_minutes", 0)),
            occupancy_off_delay_minutes=int(g.get("occupancy_off_delay_minutes", 2)),
            gating_mode=g.get("gating_mode") if g.get("gating_mode") in GATING_MODES else GATING_SLOTS,
            keep_fan_on=bool(g.get("keep_fan_on", True)),
        )

    @property
    def watched_entities(self) -> list[str]:
        out = [e for e in (self.climate_entity, self.occupancy_entity) if e]
        return out + list(self.motion_entities)


def gating_plan(mode: str, desired: bool | None, decision: str | None):
    """Map a decision to (power_command, slots_gated); either may be None = leave as is."""
    if mode != GATING_SLOTS:
        return desired, False
    if desired is None:
        return None, False
    if decision == "outside":
        return True, None
    return True, not desired


SLOTS_FALLBACK_AFTER = timedelta(seconds=90)


def slots_fallback(mode: str, desired: bool | None, work_status: str | None,
                   off_since: datetime | None, now: datetime) -> bool:
    """True when slot gating failed to stop the device and power must cut it.

    Some units (Pro300_2, firmware V1.0.20250114) keep their default 10/120 cycle with every slot
    disabled, so in ``slots`` mode a closed gate would go on diffusing. Once the device still
    reports a work cycle ``SLOTS_FALLBACK_AFTER`` after the gate closed, switch it off.
    """
    if mode != GATING_SLOTS or desired is not False or off_since is None:
        return False
    return work_status in ("diffusing", "paused") and now - off_since >= SLOTS_FALLBACK_AFTER


class GateLogic:
    """Stateful part: HVAC on-delay and the per-gate off-delay hold timestamps."""

    def __init__(self, config: GateConfig) -> None:
        self.config = config
        self._hvac_active_since: datetime | None = None
        self._hvac_false_since: datetime | None = None
        self._occ_false_since: datetime | None = None
        self.snapshot: dict = {}

    def update_hvac(self, action: str | None, now: datetime) -> None:
        if action not in HVAC_INACTIVE_ACTIONS:
            if self._hvac_active_since is None:
                self._hvac_active_since = now
        else:
            self._hvac_active_since = None

    def _hvac_gate(self, now: datetime) -> bool:
        if not self.config.climate_entity:
            return True
        if self._hvac_active_since is None:
            return False
        return now - self._hvac_active_since >= timedelta(minutes=self.config.hvac_on_delay_minutes)

    def _occupancy_gate(self, occupancy_on: bool | None) -> bool:
        return True if not self.config.occupancy_entity else bool(occupancy_on)

    def _motion_gate(self, motion: list[tuple[str, datetime]], linger_minutes: int, now: datetime) -> bool:
        if not self.config.motion_entities:
            return True      # no motion sensors: the per-day preference and master switch are the only gates
        linger = timedelta(minutes=linger_minutes)
        for state, last_changed in motion:
            if state == "on" or (state == "off" and now - last_changed <= linger):
                return True
        return False

    def _off_delay(self, ok: bool, attr: str, delay_minutes: int, now: datetime) -> bool:
        """A gate only reads false after it has been false for ``delay_minutes`` (absorbs flicker)."""
        if ok:
            setattr(self, attr, None)
            return True
        if delay_minutes <= 0:
            return False
        since = getattr(self, attr)
        if since is None:
            setattr(self, attr, now)
            since = now
        return now - since < timedelta(minutes=delay_minutes)

    def desired_power(self, model: DeviceModel, now_local: datetime, now: datetime, *,
                      timed_run_active: bool, occupancy_on: bool | None,
                      motion: list[tuple[str, datetime]]) -> bool | None:
        """True/False, or None when the engine hands off."""
        if timed_run_active:
            self.snapshot = {"decision": "timed_run"}
            return True
        if not model.schedule_enabled:
            self.snapshot = {"decision": "hands_off"}
            return None
        hit = active_window(model, now_local)
        if hit is not None:
            hvac_ok = self._hvac_gate(now)
            occ_ok = self._occupancy_gate(occupancy_on)
            hvac = self._off_delay(hvac_ok, "_hvac_false_since", self.config.hvac_off_delay_minutes, now)
            occ = self._off_delay(occ_ok, "_occ_false_since", self.config.occupancy_off_delay_minutes, now)
            desired = hvac and occ
            self.snapshot = {
                "decision": "window", "window": f"{hit[2].start}-{hit[2].end}",
                "hvac": hvac_ok, "hvac_configured": bool(self.config.climate_entity),
                "occupancy": occ_ok, "occupancy_configured": bool(self.config.occupancy_entity),
                "holding": desired and not (hvac_ok and occ_ok),
            }
            return desired
        self._hvac_false_since = self._occ_false_since = None   # stale holds must not cut the next window
        if night_owl_period(model, now_local):
            ok = self._motion_gate(motion, model.night_owl.linger_minutes, now)
            self.snapshot = {"decision": "night_owl", "motion": ok}
            return ok
        self.snapshot = {"decision": "outside"}
        return False
