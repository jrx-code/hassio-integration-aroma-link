"""Pure data model of the desired state HA keeps per device (no Home Assistant imports).

Adapted from cjam28/homeassistant_aroma-link v3.1.2 (MIT): the weekly schedule, Night Owl
settings and flags. The device itself holds ONE set of 5 slots, so HA owns the weekly
logic and writes the current day's slots (see ``schedule.py``).

Slot doctrine: slots 1-4 carry the user's windows, slot 5 carries Night Owl or the
timed-run overlay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import IntEnum

SLOT_COUNT = 5
MAX_WINDOWS = 4
NIGHT_OWL_SLOT_INDEX = 4   # 0-based position of slot 5

WORK_SEC_MIN, WORK_SEC_MAX = 5, 900
PAUSE_SEC_MIN, PAUSE_SEC_MAX = 5, 900


class Weekday(IntEnum):
    """Monday=0 .. Sunday=6, identical to ``datetime.weekday()``."""

    MON = 0
    TUE = 1
    WED = 2
    THU = 3
    FRI = 4
    SAT = 5
    SUN = 6

    @property
    def prev(self) -> Weekday:
        return Weekday((int(self) - 1) % 7)

    @property
    def next(self) -> Weekday:
        return Weekday((int(self) + 1) % 7)


def today(now: datetime) -> Weekday:
    return Weekday(now.weekday())


def parse_hhmm(value: str) -> time:
    """'HH:MM'; '24:00' is accepted as end of day (23:59)."""
    hh, mm = value.split(":")
    if int(hh) == 24 and int(mm) == 0:
        return time(23, 59)
    return time(int(hh), int(mm))


def is_hhmm(value) -> bool:
    try:
        parse_hhmm(value)
        return isinstance(value, str)
    except (ValueError, AttributeError, TypeError):
        return False


@dataclass(frozen=True)
class ScheduleWindow:
    """One normal diffusing window (device slots 1-4). Must not cross midnight."""

    start: str
    end: str
    work_sec: int
    pause_sec: int
    level: int = 1          # 1|2|3 (A|B|C)
    enabled: bool = True

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "work_sec": self.work_sec,
                "pause_sec": self.pause_sec, "level": self.level, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, data: dict) -> ScheduleWindow:
        return cls(start=str(data["start"]), end=str(data["end"]), work_sec=int(data["work_sec"]),
                   pause_sec=int(data["pause_sec"]), level=int(data.get("level", 1)),
                   enabled=bool(data.get("enabled", True)))

    def contains(self, t: time) -> bool:
        return parse_hhmm(self.start) <= t < parse_hhmm(self.end)


@dataclass
class DaySchedule:
    """Windows plus the Night Owl preference for the night that STARTS this evening."""

    windows: list[ScheduleWindow] = field(default_factory=list)
    night_owl: bool = False

    def enabled_windows(self) -> list[ScheduleWindow]:
        return [w for w in self.windows if w.enabled]

    def to_dict(self) -> dict:
        return {"windows": [w.to_dict() for w in self.windows], "night_owl": self.night_owl}

    @classmethod
    def from_dict(cls, data: dict) -> DaySchedule:
        return cls(windows=[ScheduleWindow.from_dict(w) for w in data.get("windows", [])][:MAX_WINDOWS],
                   night_owl=bool(data.get("night_owl", False)))


@dataclass
class NightOwlSettings:
    """Device-level Night Owl configuration (per-day allow flags live on DaySchedule)."""

    mode: str = "outside_windows"   # "outside_windows" | "fixed"
    fixed_start: str = "22:00"
    fixed_end: str = "06:00"
    work_sec: int = 10
    pause_sec: int = 300
    level: int = 1
    linger_minutes: int = 10

    def to_dict(self) -> dict:
        return dict(vars(self))

    @classmethod
    def from_dict(cls, data: dict) -> NightOwlSettings:
        base = cls()
        return cls(
            mode=str(data.get("mode", base.mode)),
            fixed_start=str(data.get("fixed_start", base.fixed_start)),
            fixed_end=str(data.get("fixed_end", base.fixed_end)),
            work_sec=int(data.get("work_sec", base.work_sec)),
            pause_sec=int(data.get("pause_sec", base.pause_sec)),
            level=int(data.get("level", base.level)),
            linger_minutes=int(data.get("linger_minutes", base.linger_minutes)),
        )


@dataclass
class WeeklySchedule:
    days: dict[Weekday, DaySchedule] = field(
        default_factory=lambda: {d: DaySchedule() for d in Weekday})
    version: int = 0
    updated_at: str = ""   # ISO UTC

    def to_dict(self) -> dict:
        return {"version": self.version, "updated_at": self.updated_at,
                "days": {str(int(d)): self.days[d].to_dict() for d in Weekday}}

    @classmethod
    def from_dict(cls, data: dict) -> WeeklySchedule:
        days = {d: DaySchedule() for d in Weekday}
        for key, day_data in (data.get("days") or {}).items():
            days[Weekday(int(key))] = DaySchedule.from_dict(day_data)
        return cls(days=days, version=int(data.get("version", 0)), updated_at=str(data.get("updated_at", "")))


@dataclass
class TimedRun:
    """A persisted 'run for N minutes': survives HA restarts."""

    ends_at: str                       # ISO UTC
    work_sec: int
    pause_sec: int
    duration_minutes: int
    prior_power: bool | None = None    # power before the first run; restored on expiry when hands-off

    def to_dict(self) -> dict:
        return dict(vars(self))

    @classmethod
    def from_dict(cls, data: dict) -> TimedRun:
        return cls(ends_at=str(data["ends_at"]), work_sec=int(data["work_sec"]), pause_sec=int(data["pause_sec"]),
                   duration_minutes=int(data.get("duration_minutes", 0)), prior_power=data.get("prior_power"))


@dataclass
class DeviceModel:
    """Everything HA persists about one device's desired state."""

    schedule: WeeklySchedule = field(default_factory=WeeklySchedule)
    night_owl: NightOwlSettings = field(default_factory=NightOwlSettings)
    schedule_enabled: bool = True     # master switch: HA applies the schedule and gating
    night_owl_enabled: bool = True
    default_work_sec: int = 10        # defaults for timed runs and new windows
    default_pause_sec: int = 300
    timed_run: TimedRun | None = None
    usage: dict = field(default_factory=dict)   # see usage.py
    oil: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"schedule": self.schedule.to_dict(), "night_owl": self.night_owl.to_dict(),
                "schedule_enabled": self.schedule_enabled, "night_owl_enabled": self.night_owl_enabled,
                "default_work_sec": self.default_work_sec, "default_pause_sec": self.default_pause_sec,
                "timed_run": self.timed_run.to_dict() if self.timed_run else None,
                "usage": self.usage, "oil": self.oil}

    @classmethod
    def from_dict(cls, data: dict) -> DeviceModel:
        return cls(
            schedule=WeeklySchedule.from_dict(data.get("schedule") or {}),
            night_owl=NightOwlSettings.from_dict(data.get("night_owl") or {}),
            schedule_enabled=bool(data.get("schedule_enabled", True)),
            night_owl_enabled=bool(data.get("night_owl_enabled", True)),
            default_work_sec=int(data.get("default_work_sec", 10)),
            default_pause_sec=int(data.get("default_pause_sec", 300)),
            timed_run=TimedRun.from_dict(data["timed_run"]) if data.get("timed_run") else None,
            usage=dict(data.get("usage") or {}), oil=dict(data.get("oil") or {}),
        )
