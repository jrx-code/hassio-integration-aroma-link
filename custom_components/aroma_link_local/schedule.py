"""Pure schedule logic: validate the model, compile one day into the device's 5 slots.

The device keeps ONE set of 5 slots (no weekday, verified by probing), so HA compiles the
current day and rewrites the slots when the model changes and at midnight.
Slots 1-4 carry the day's windows, slot 5 the Night Owl window or a timed-run overlay.
Adapted from cjam28/homeassistant_aroma-link v3.1.2 (MIT).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time

from .aroma_codec import FILLER, Slot
from .model import (
    MAX_WINDOWS,
    NIGHT_OWL_SLOT_INDEX,
    PAUSE_SEC_MAX,
    PAUSE_SEC_MIN,
    WORK_SEC_MAX,
    WORK_SEC_MIN,
    DeviceModel,
    NightOwlSettings,
    ScheduleWindow,
    Weekday,
    WeeklySchedule,
    is_hhmm,
    parse_hhmm,
    today,
)


def window_to_slot(window: ScheduleWindow) -> Slot:
    start, end = parse_hhmm(window.start), parse_hhmm(window.end)
    return Slot(start.hour, start.minute, end.hour, end.minute, window.level,
                window.enabled, window.work_sec, window.pause_sec)


def slot_to_window(slot: Slot) -> ScheduleWindow:
    return ScheduleWindow(f"{slot.start_h:02d}:{slot.start_m:02d}", f"{slot.end_h:02d}:{slot.end_m:02d}",
                          slot.work_sec, slot.pause_sec, slot.level, slot.enabled)


def validate_schedule(schedule: WeeklySchedule) -> list[str]:
    """Human-readable problems; an empty list means valid."""
    errors: list[str] = []
    for day in Weekday:
        day_sched = schedule.days.get(day)
        if day_sched is None:
            errors.append(f"{day.name}: missing day")
            continue
        if len(day_sched.windows) > MAX_WINDOWS:
            errors.append(f"{day.name}: more than {MAX_WINDOWS} windows")
        for i, w in enumerate(day_sched.windows, start=1):
            label = f"{day.name} window {i}"
            if not is_hhmm(w.start) or not is_hhmm(w.end):
                errors.append(f"{label}: invalid time format")
                continue
            if parse_hhmm(w.end) <= parse_hhmm(w.start):
                errors.append(f"{label}: end must be after start (no midnight crossing)")
            if not WORK_SEC_MIN <= w.work_sec <= WORK_SEC_MAX:
                errors.append(f"{label}: work_sec out of range {WORK_SEC_MIN}-{WORK_SEC_MAX}")
            if not PAUSE_SEC_MIN <= w.pause_sec <= PAUSE_SEC_MAX:
                errors.append(f"{label}: pause_sec out of range {PAUSE_SEC_MIN}-{PAUSE_SEC_MAX}")
            if w.level not in (1, 2, 3):
                errors.append(f"{label}: level must be 1, 2, or 3")
        ok = [w for w in day_sched.windows if w.enabled and is_hhmm(w.start) and is_hhmm(w.end)
              and parse_hhmm(w.start) < parse_hhmm(w.end)]
        for i in range(len(ok)):
            for j in range(i + 1, len(ok)):
                a, b = ok[i], ok[j]
                if parse_hhmm(a.start) < parse_hhmm(b.end) and parse_hhmm(b.start) < parse_hhmm(a.end):
                    errors.append(f"{day.name}: windows {a.start}-{a.end} and {b.start}-{b.end} overlap")
    return errors


def _fixed_crosses_midnight(settings: NightOwlSettings) -> bool:
    return parse_hhmm(settings.fixed_end) <= parse_hhmm(settings.fixed_start)


def night_owl_slot(model: DeviceModel, day: Weekday) -> Slot:
    """Slot 5 for ``day``: a deliberate SUPERSET of when Night Owl may run.

    Power gating decides when the device really runs, so over-enabling the slot is harmless
    while under-enabling would block legitimate runs. A midnight-crossing fixed window or the
    outside_windows mode covers both this evening and the morning tail of the previous night.
    """
    cfg, days = model.night_owl, model.schedule.days
    if not model.night_owl_enabled:
        allowed = False
    elif cfg.mode == "fixed" and not _fixed_crosses_midnight(cfg):
        allowed = days[day].night_owl
    else:
        allowed = days[day].night_owl or days[day.prev].night_owl
    if cfg.mode == "fixed":
        start, end = parse_hhmm(cfg.fixed_start), parse_hhmm(cfg.fixed_end)
        if end <= start:                  # crosses midnight: the device slot cannot, so cover the day
            start, end = time(0, 0), time(23, 59)
    else:
        start, end = time(0, 0), time(23, 59)
    return Slot(start.hour, start.minute, end.hour, end.minute, cfg.level, allowed, cfg.work_sec, cfg.pause_sec)


def owning_evening(model: DeviceModel, now: datetime) -> Weekday | None:
    """Which evening's Night Owl preference governs ``now``, or None when it is not night.

    fixed: inside the fixed window only (evening part -> today, morning part of a midnight
    crossing window -> yesterday). outside_windows: after today's last enabled window -> today,
    before the first -> yesterday, gaps between two windows are not night. A day without enabled
    windows splits at noon.
    """
    cfg, day, t = model.night_owl, today(now), now.time()
    if cfg.mode == "fixed":
        start, end = parse_hhmm(cfg.fixed_start), parse_hhmm(cfg.fixed_end)
        if start < end:
            return day if start <= t < end else None
        if t >= start:
            return day
        return day.prev if t < end else None
    enabled = model.schedule.days[day].enabled_windows()
    if not enabled:
        return day.prev if t < time(12, 0) else day
    first = min(parse_hhmm(w.start) for w in enabled)
    last = max(parse_hhmm(w.end) for w in enabled)
    if t >= last:
        return day
    if t < first:
        return day.prev
    return None


def night_owl_period(model: DeviceModel, now: datetime) -> bool:
    """True when ``now`` falls in a Night Owl period whose owning evening allows it."""
    if not model.night_owl_enabled or active_window(model, now) is not None:
        return False
    owner = owning_evening(model, now)
    return owner is not None and model.schedule.days[owner].night_owl


def compile_day(model: DeviceModel, day: Weekday, overlay: Slot | None = None,
                gated_off: bool = False) -> list[Slot]:
    """The 5 slots the device should hold for ``day``.

    ``overlay`` (timed run) replaces slot 5 with a 24/7 enabled window.
    ``gated_off`` (schedule-flip gating) disarms every slot so a powered device stays idle.
    """
    slots = [window_to_slot(w) for w in model.schedule.days[day].windows[:MAX_WINDOWS]]
    slots += [FILLER] * (MAX_WINDOWS - len(slots))
    slots.append(night_owl_slot(model, day))
    if gated_off:
        slots = [replace(s, enabled=False) for s in slots]
    if overlay is not None:
        slots[NIGHT_OWL_SLOT_INDEX] = overlay
    return slots


def active_window(model: DeviceModel, now: datetime):
    """(day, index, window) when ``now`` is inside an enabled window, else None."""
    day = today(now)
    for idx, window in enumerate(model.schedule.days[day].windows):
        if window.enabled and window.contains(now.time()):
            return day, idx, window
    return None


def schedule_from_slots(slots: list[Slot]) -> WeeklySchedule:
    """Use the device's slots 1-4 as the window set of every weekday (import).

    Unused slots (the filler, or zero-length windows such as 00:00-00:00 that the cloud left
    enabled) are skipped so the imported model validates.
    """
    windows = [slot_to_window(s) for s in slots[:MAX_WINDOWS]
               if s != FILLER and (s.start_h, s.start_m) < (s.end_h, s.end_m)]
    sched = WeeklySchedule()
    for day in Weekday:
        sched.days[day].windows = list(windows)
    return sched


def capability(model: DeviceModel, now: datetime, overlay_active: bool = False,
               gated_off: bool = False) -> str | None:
    """Why the device is allowed to diffuse right now: timed_run | window | night_owl | None."""
    if overlay_active:
        return "timed_run"
    if gated_off:
        return None
    if active_window(model, now) is not None:
        return "window"
    if night_owl_period(model, now):
        return "night_owl"
    return None


def overlay_slot(work_sec: int, pause_sec: int, level: int = 1) -> Slot:
    """The timed-run overlay: slot 5 becomes a 24/7 enabled window."""
    return Slot(0, 0, 23, 59, level, True, work_sec, pause_sec)


def timed_run_expiry_power(schedule_enabled: bool, prior_power: bool | None) -> bool | None:
    """Power command at expiry (None = send nothing).

    With the schedule enabled the gating engine owns power and re-evaluates once the run
    clears, so a run inside an active window does not cut the device off. A hands-off device
    goes back to its pre-run state; an unknown prior state means off.
    """
    if schedule_enabled:
        return None
    return bool(prior_power)


def scheduled_work_seconds_per_day(model: DeviceModel, day: Weekday) -> float:
    """Pump work seconds a day's enabled windows would produce at their duty cycle."""
    total = 0.0
    for w in model.schedule.days[day].enabled_windows():
        minutes = (parse_hhmm(w.end).hour * 60 + parse_hhmm(w.end).minute) - (parse_hhmm(w.start).hour * 60 + parse_hhmm(w.start).minute)
        if minutes > 0:
            total += minutes * 60 * w.work_sec / (w.work_sec + w.pause_sec)
    return total
