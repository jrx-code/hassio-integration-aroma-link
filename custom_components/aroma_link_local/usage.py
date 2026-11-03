"""Usage counters and oil tracking (pure, no Home Assistant imports).

The local protocol reports no counters (the cloud's pumpCount/runCount came from the portal),
so they are derived from what the device does report: power and the work phase from the 52/25
status report. While the device is powered AND cycling, elapsed time is split by the active
slot's work/pause durations. This is an estimate whose error is bounded by the poll interval.
Oil follows cjam28/homeassistant_aroma-link v3.1.2 (MIT): fill, calibrate the usage rate,
estimate the remainder.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MAX_GAP = 300.0          # seconds; longer gaps (HA down, device offline) are not counted
# Until calibrated, the level is estimated at a nominal nebulizer rate: no Pro300 figure is
# published; AromaPro nebulizers state 1-2 ml per hour of continuous spray (FCC filing).
DEFAULT_RATE = 1.5 / 3600.0   # ml per second of pump work


@dataclass
class Usage:
    work_seconds: float = 0.0       # estimated seconds the pump was working (lifetime)
    cycles: float = 0.0             # estimated completed work/pause cycles (lifetime)
    on_count: int = 0               # power OFF -> ON transitions seen
    work_since_fill: float = 0.0    # work seconds since the last refill (oil basis)
    last_ts: float | None = None
    last_power: bool | None = None
    last_cycling: bool = False

    def to_dict(self) -> dict:
        return dict(vars(self))

    @classmethod
    def from_dict(cls, data: dict) -> Usage:
        base = cls()
        return cls(**{k: data.get(k, getattr(base, k)) for k in vars(base)})

    @property
    def pump_count(self) -> int:
        return int(self.cycles)


def sample(u: Usage, ts: float, power: bool, phase: str, work_sec: int, pause_sec: int) -> bool:
    """Feed one status sample; returns True when the counters changed."""
    changed = False
    cycling = bool(power) and phase in ("diffusing", "paused")
    if u.last_power is not None and power and not u.last_power:
        u.on_count += 1
        changed = True
    if u.last_ts is not None and u.last_cycling and cycling:
        dt = min(ts - u.last_ts, MAX_GAP)
        period = max(work_sec + pause_sec, 1)
        if dt > 0:
            worked = dt * work_sec / period
            u.work_seconds += worked
            u.work_since_fill += worked
            u.cycles += dt / period
            changed = True
    if (u.last_power, u.last_cycling) != (power, cycling):
        changed = True
    u.last_ts, u.last_power, u.last_cycling = ts, bool(power), cycling
    return changed


@dataclass
class Oil:
    bottle_capacity: float = 300.0      # ml; Pro300 holds 300, other models 400 or 1000 (see capacity_from_model)
    fill_volume: float = 300.0          # ml in the bottle at the last refill
    fill_date: str | None = None
    usage_rate: float | None = None     # ml per second of pump work (None = not calibrated)
    calibration_state: str = "Idle"     # Idle | Measuring | Ready to Finalize | Calibrated
    calibration_method: str = "measured"
    measured_remaining: float | None = None
    manual_rate_ml_per_hour: float | None = None
    manual_start_volume: float | None = None
    manual_end_volume: float | None = None
    manual_runtime_hours: float | None = None
    events: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(vars(self))

    @classmethod
    def from_dict(cls, data: dict) -> Oil:
        base = cls()
        return cls(**{k: data.get(k, getattr(base, k)) for k in vars(base)})


def capacity_from_model(model: str | None) -> float | None:
    """Bottle ml from a model string such as 'A6/Pro300_2_SEG' (Pro300 -> 300); None if unknown."""
    import re
    m = re.search(r"Pro(\d{3,4})", model or "")
    return float(m.group(1)) if m else None


SETTABLE = ("bottle_capacity", "fill_volume", "measured_remaining", "manual_rate_ml_per_hour",
            "manual_start_volume", "manual_end_volume", "manual_runtime_hours")


def log_event(oil: Oil, kind: str, detail: str, now_iso: str) -> None:
    oil.events = (oil.events + [[now_iso, kind, detail]])[-20:]


def effective_rate(oil: Oil) -> float:
    """Calibrated ml per work second, else the nominal DEFAULT_RATE."""
    return oil.usage_rate if oil.usage_rate else DEFAULT_RATE


def rate_source(oil: Oil) -> str:
    return "calibrated" if oil.usage_rate else "default"


def set_capacity(oil: Oil, capacity: float) -> None:
    """New bottle size; the fill volume can never exceed the bottle."""
    oil.bottle_capacity = float(capacity)
    oil.fill_volume = min(oil.fill_volume, oil.bottle_capacity)


def remaining_ml(oil: Oil, work_since_fill: float) -> float:
    return max(0.0, min(oil.fill_volume, oil.bottle_capacity) - effective_rate(oil) * work_since_fill)


def level_percent(oil: Oil, work_since_fill: float) -> float | None:
    if oil.bottle_capacity <= 0:
        return None
    return min(100.0, remaining_ml(oil, work_since_fill) / oil.bottle_capacity * 100.0)


def days_remaining(oil: Oil, work_since_fill: float, work_seconds_per_day: float) -> float | None:
    if work_seconds_per_day <= 0:
        return None
    return remaining_ml(oil, work_since_fill) / (effective_rate(oil) * work_seconds_per_day)


def refill(oil: Oil, usage: Usage, now_iso: str, keep_calibration: bool = True,
           fill_volume: float | None = None, fill_date: str | None = None) -> None:
    """New bottle: restart the oil basis. ``keep_calibration=False`` also forgets the rate."""
    if fill_volume is not None:
        oil.fill_volume = float(fill_volume)
    elif oil.bottle_capacity:
        oil.fill_volume = oil.bottle_capacity
    oil.fill_date = fill_date or now_iso
    usage.work_since_fill = 0.0
    if not keep_calibration:
        oil.usage_rate, oil.calibration_state, oil.measured_remaining = None, "Idle", None
    log_event(oil, "REFILL", f"{oil.fill_volume} ml, calibration {'kept' if keep_calibration else 'reset'}", now_iso)


def calibrate(oil: Oil, usage: Usage, action: str, now_iso: str) -> str | None:
    """start | end | finalize | manual | set. Returns a problem text, or None when fine."""
    if action == "start":
        oil.calibration_state = "Measuring"
        log_event(oil, "CAL_START", "measuring", now_iso)
    elif action == "end":
        if oil.calibration_state != "Measuring":
            return "calibration is not in the Measuring state"
        oil.calibration_state = "Ready to Finalize"
        log_event(oil, "CAL_END", f"{usage.work_since_fill:.0f} s of work", now_iso)
    elif action == "finalize":
        if oil.calibration_state != "Ready to Finalize":
            return f"cannot finalize from state {oil.calibration_state}"
        if oil.measured_remaining is None or usage.work_since_fill <= 0:
            return "set measured_remaining and run the diffuser first"
        used = oil.fill_volume - oil.measured_remaining
        if used <= 0:
            return "measured_remaining must be below fill_volume"
        oil.usage_rate = used / usage.work_since_fill
        oil.calibration_state, oil.calibration_method = "Calibrated", "measured"
        log_event(oil, "CAL_FINAL", f"{oil.usage_rate:.6f} ml/s", now_iso)
    elif action == "manual":
        rate = oil.manual_rate_ml_per_hour or 0
        if rate > 0:
            oil.usage_rate, oil.calibration_method = rate / 3600.0, "manual_rate"
        else:
            hours = oil.manual_runtime_hours or 0
            used = (oil.manual_start_volume or 0) - (oil.manual_end_volume or 0)
            if hours <= 0 or used <= 0:
                return "manual needs a rate, or start/end volumes with the runtime hours"
            oil.usage_rate, oil.calibration_method = used / (hours * 3600.0), "manual_volumes"
        oil.calibration_state = "Calibrated"
        log_event(oil, "CAL_MANUAL", f"{oil.usage_rate * 3600:.3f} ml/h", now_iso)
    elif action != "set":
        return f"unknown action {action}"
    return None
