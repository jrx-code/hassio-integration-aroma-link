"""Pure payload builders for the schedule card's websocket API (no Home Assistant imports)."""
from __future__ import annotations

from datetime import datetime

from .model import DeviceModel, parse_hhmm
from .schedule import active_window

# The card treats work_status 2 as "diffusing".
WORK_NUMBER = {"off": 0, "idle": 0, "paused": 1, "diffusing": 2}
CHANGE_FOR_MODEL = {"schedule": "schedule", "flags": "flags", "night_owl": "night_owl",
                    "defaults": "flags", "timed_run_state": "timed_run", "usage": "oil"}


def sync_state(managed: bool, in_sync: bool, error: str | None) -> dict:
    return {"synced": bool(managed and in_sync and not error),
            "pending": bool(managed and not in_sync and not error), "error": error}


def window_payload(model: DeviceModel, now_local: datetime) -> dict | None:
    hit = active_window(model, now_local)
    return None if hit is None else {"day": int(hit[0]), "index": hit[1], "start": hit[2].start, "end": hit[2].end}


def timed_run_payload(run, now_utc: datetime) -> dict | None:
    if run is None:
        return None
    ends = datetime.fromisoformat(run.ends_at)
    remaining = max(0, int((ends - now_utc).total_seconds()))
    return {"active": remaining > 0, "ends_at": run.ends_at, "remaining_s": remaining,
            "duration_minutes": run.duration_minutes, "work_sec": run.work_sec, "pause_sec": run.pause_sec}


def oil_payload(oil, level, remaining, days, work_since_fill: float) -> dict:
    return {"level_pct": None if level is None else round(level, 1),
            "remaining_ml": None if remaining is None else round(remaining, 1),
            "days_remaining": None if days is None else round(days, 1),
            "usage_rate_ml_per_hour": round(oil.usage_rate * 3600, 4) if oil.usage_rate else None,
            "calibrated": oil.usage_rate is not None, "calibration_state": oil.calibration_state,
            "rate_source": "calibrated" if oil.usage_rate else "default",
            "fill_date": oil.fill_date, "bottle_capacity_ml": oil.bottle_capacity,
            "fill_volume_ml": oil.fill_volume, "runtime_since_fill_hours": round(work_since_fill / 3600, 2)}


def validate_night_owl(data: dict) -> list[str]:
    errors = []
    if data.get("mode", "outside_windows") not in ("outside_windows", "fixed"):
        errors.append("night_owl.mode must be 'outside_windows' or 'fixed'")
    for key in ("fixed_start", "fixed_end"):
        if data.get(key) is not None:
            try:
                parse_hhmm(str(data[key]))
            except (ValueError, AttributeError):
                errors.append(f"night_owl.{key} must be HH:MM")
    for key, low, high in (("work_sec", 5, 900), ("pause_sec", 5, 900), ("linger_minutes", 1, 240)):
        if data.get(key) is not None and not low <= int(data[key]) <= high:
            errors.append(f"night_owl.{key} out of range {low}-{high}")
    if data.get("level") is not None and int(data["level"]) not in (1, 2, 3):
        errors.append("night_owl.level must be 1, 2, or 3")
    return errors
