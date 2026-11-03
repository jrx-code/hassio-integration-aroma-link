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


_LAST_MINUTE = 23 * 60 + 59
_DAY_SEC = 86400


def day_drift(drift_sec: float) -> float:
    """Clock difference reduced to the time of day, in [-12 h, 12 h).

    Slots carry only hours and minutes, so a clock that is days or years off (a device the cloud
    never set, e.g. 2021-05-27 on a new Pro300) matters only by its time-of-day difference.
    """
    return (drift_sec + _DAY_SEC / 2) % _DAY_SEC - _DAY_SEC / 2


def offset_minutes(drift_sec: float | None) -> int:
    """Whole minutes the device clock is ahead of real time in the day (negative = behind)."""
    if drift_sec is None:
        return 0
    return round(day_drift(drift_sec) / 60)


def _minutes(hh: int, mm: int) -> int:
    return hh * 60 + mm


def _shift_windows(model: DeviceModel, device_day: Weekday, offset_min: int) -> list[Slot]:
    """Windows to put in slots 1-4 when the device clock is ``offset_min`` ahead of real time.

    A real window [s, e] shows up as [s + offset, e + offset] on the device clock. The device
    day does not start at real midnight, so the tail of one real day can land on the next device
    day (and with a negative offset the head of the next real day on the previous one): the
    neighbouring real days are shifted into this device day and clipped to it.
    """
    pieces: list[Slot] = []
    for k, real_day in ((-1, device_day.prev), (0, device_day), (1, device_day.next)):
        for w in model.schedule.days[real_day].windows[:MAX_WINDOWS]:
            if k != 0 and not w.enabled:
                continue
            start = _minutes_of(w.start) + offset_min + 1440 * k
            end = _minutes_of(w.end) + offset_min + 1440 * k
            start, end = max(start, 0), min(end, _LAST_MINUTE)
            if end <= start:
                continue
            pieces.append(Slot(start // 60, start % 60, end // 60, end % 60,
                               w.level, w.enabled, w.work_sec, w.pause_sec))
    pieces.sort(key=lambda s: (s.start_h, s.start_m))
    merged: list[Slot] = []
    for s in pieces:
        prev = merged[-1] if merged else None
        if (prev is not None and prev.enabled == s.enabled and prev.level == s.level
                and prev.work_sec == s.work_sec and prev.pause_sec == s.pause_sec
                and _minutes(s.start_h, s.start_m) <= _minutes(prev.end_h, prev.end_m) + 1):
            end_h, end_m = max((prev.end_h, prev.end_m), (s.end_h, s.end_m))
            merged[-1] = replace(prev, end_h=end_h, end_m=end_m)
        else:
            merged.append(s)
    return merged[:MAX_WINDOWS]


def _minutes_of(hhmm: str) -> int:
    t = parse_hhmm(hhmm)
    return t.hour * 60 + t.minute


def compile_day_for_device(model: DeviceModel, device_day: Weekday, offset_min: int,
                           overlay: Slot | None = None, gated_off: bool = False) -> list[Slot]:
    """Like ``compile_day`` but in the device's own clock, ``offset_min`` ahead of real time.

    ``device_day`` is the weekday the device clock shows now. With a zero offset the result is
    identical to ``compile_day``.
    """
    if offset_min == 0:
        return compile_day(model, device_day, overlay, gated_off)
    windows = _shift_windows(model, device_day, offset_min)
    slots = windows + [FILLER] * (MAX_WINDOWS - len(windows))
    owl = night_owl_slot(model, device_day)
    cfg = model.night_owl
    if cfg.mode == "fixed" and not _fixed_crosses_midnight(cfg):
        start = max(_minutes_of(cfg.fixed_start) + offset_min, 0)
        end = min(_minutes_of(cfg.fixed_end) + offset_min, _LAST_MINUTE)
        if end > start:
            owl = replace(owl, start_h=start // 60, start_m=start % 60, end_h=end // 60, end_m=end % 60)
    slots.append(owl)
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


def schedule_from_slots(slots: list[Slot], offset_min: int = 0) -> WeeklySchedule:
    """Use the device's slots 1-4 as the window set of every weekday (import).

    Unused slots (the filler, or zero-length windows such as 00:00-00:00 that the cloud left
    enabled) are skipped so the imported model validates. The slots are in the device's clock,
    ``offset_min`` ahead of real time, so they are shifted back and clipped to the day.
    """
    windows = []
    for s in slots[:MAX_WINDOWS]:
        if s == FILLER or (s.start_h, s.start_m) >= (s.end_h, s.end_m):
            continue
        start = max(_minutes(s.start_h, s.start_m) - offset_min, 0)
        end = min(_minutes(s.end_h, s.end_m) - offset_min, _LAST_MINUTE)
        if end > start:
            windows.append(slot_to_window(replace(s, start_h=start // 60, start_m=start % 60,
                                                  end_h=end // 60, end_m=end % 60)))
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
