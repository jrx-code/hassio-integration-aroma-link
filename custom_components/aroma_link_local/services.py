"""Domain services: sync_schedules, set_schedule, import_schedule."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from . import aroma_codec as codec
from .const import DOMAIN
from .model import NightOwlSettings, WeeklySchedule, is_hhmm
from .schedule import schedule_from_slots, validate_schedule
from .usage import SETTABLE, calibrate, refill

DEVICE = {vol.Optional("device_id"): cv.string}
SYNC_SCHEMA = vol.Schema(DEVICE)
SET_SCHEDULE_SCHEMA = vol.Schema({**DEVICE, vol.Required("schedule"): dict})
IMPORT_SCHEMA = vol.Schema(DEVICE)
START_RUN_SCHEMA = vol.Schema({
    **DEVICE,
    vol.Required("duration_minutes"): vol.All(int, vol.Range(min=1, max=1440)),
    vol.Optional("work_sec"): vol.All(int, vol.Range(min=5, max=900)),
    vol.Optional("pause_sec"): vol.All(int, vol.Range(min=5, max=900)),
})
OIL_REFILL_SCHEMA = vol.Schema({
    **DEVICE,
    vol.Optional("fill_volume"): vol.Coerce(float),
    vol.Optional("fill_date"): cv.string,
    vol.Optional("keep_calibration", default=True): cv.boolean,
})
OIL_CALIBRATE_SCHEMA = vol.Schema({
    **DEVICE,
    vol.Required("action"): vol.In(["start", "end", "finalize", "manual", "set"]),
    **{vol.Optional(k): vol.Coerce(float) for k in SETTABLE},
})
SEND_FRAME_SCHEMA = vol.Schema({
    **DEVICE,
    vol.Required("cmd"): vol.All(cv.string, vol.Match(r"^[0-9A-Fa-f]{2}$")),
    vol.Required("sub"): vol.All(cv.string, vol.Match(r"^[0-9A-Fa-f]{2}$")),
    vol.Optional("payload", default=""): vol.All(cv.string, vol.Match(r"^([0-9A-Fa-f]{2})*$")),
})
NIGHT_OWL_SCHEMA = vol.Schema({
    **DEVICE,
    vol.Optional("mode"): vol.In(["outside_windows", "fixed"]),
    vol.Optional("fixed_start"): cv.string,
    vol.Optional("fixed_end"): cv.string,
    vol.Optional("work_sec"): vol.All(int, vol.Range(min=5, max=900)),
    vol.Optional("pause_sec"): vol.All(int, vol.Range(min=5, max=900)),
    vol.Optional("level"): vol.In([1, 2, 3]),
    vol.Optional("linger_minutes"): vol.All(int, vol.Range(min=0, max=1440)),
})


def _hubs(hass: HomeAssistant):
    return [h for h in hass.data.get(DOMAIN, {}).values() if hasattr(h, "devices")]


def resolve(hass: HomeAssistant, call: ServiceCall):
    """(hub, device) for a service call; ``device_id`` is optional with a single device."""
    wanted = call.data.get("device_id")
    found = []
    for hub in _hubs(hass):
        for dev in hub.devices.values():
            if wanted is None:
                found.append((hub, dev))
                continue
            mac_id = dev.mac.replace(":", "").lower()
            reg = dr.async_get(hass).async_get(wanted)
            if wanted in (dev.topic_id, dev.mac, mac_id) or (reg and (DOMAIN, mac_id) in reg.identifiers):
                found.append((hub, dev))
    if len(found) == 1:
        return found[0]
    raise vol.Invalid("unknown device" if not found else "several devices, pass device_id")


def async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "sync_schedules"):
        return

    async def sync_schedules(call: ServiceCall) -> None:
        hub, dev = resolve(hass, call)
        rec = hub.reconciler(dev)
        rec.reset_budget()
        await rec.async_check(force=True)

    async def set_schedule(call: ServiceCall) -> None:
        hub, dev = resolve(hass, call)
        try:
            schedule = WeeklySchedule.from_dict(call.data["schedule"])
        except (KeyError, ValueError, TypeError) as err:
            raise vol.Invalid(f"bad schedule: {err}") from err
        errors = validate_schedule(schedule)
        if errors:
            raise vol.Invalid("; ".join(errors))
        model = hub.models.get(dev.topic_id)
        schedule.version = model.schedule.version + 1
        schedule.updated_at = dt_util.utcnow().isoformat()
        model.schedule = schedule
        hub.models.changed(dev.topic_id, "schedule")

    async def import_schedule(call: ServiceCall) -> None:
        hub, dev = resolve(hass, call)
        if not dev.slots:
            raise vol.Invalid("no schedule dump from the device yet")
        model = hub.models.get(dev.topic_id)
        schedule = schedule_from_slots(dev.slots)
        schedule.version = model.schedule.version + 1
        schedule.updated_at = dt_util.utcnow().isoformat()
        model.schedule = schedule
        hub.models.changed(dev.topic_id, "schedule")

    async def set_night_owl(call: ServiceCall) -> None:
        hub, dev = resolve(hass, call)
        data = {k: v for k, v in call.data.items() if k != "device_id"}
        for key in ("fixed_start", "fixed_end"):
            if key in data and not is_hhmm(data[key]):
                raise vol.Invalid(f"{key} must be HH:MM")
        model = hub.models.get(dev.topic_id)
        model.night_owl = NightOwlSettings.from_dict({**model.night_owl.to_dict(), **data})
        hub.models.changed(dev.topic_id, "night_owl")

    async def start_timed_run(call: ServiceCall) -> None:
        hub, dev = resolve(hass, call)
        await hub.timed.async_start(dev, call.data["duration_minutes"], call.data.get("work_sec"),
                                    call.data.get("pause_sec"))

    async def cancel_timed_run(call: ServiceCall) -> None:
        hub, dev = resolve(hass, call)
        await hub.timed.async_cancel(dev)

    async def oil_refill(call: ServiceCall) -> None:
        hub, dev = resolve(hass, call)
        usage, oil = hub.usage(dev), hub.oil(dev)
        refill(oil, usage, dt_util.utcnow().isoformat(), call.data["keep_calibration"],
               call.data.get("fill_volume"), call.data.get("fill_date"))
        hub.save_usage(dev, usage=usage, oil=oil)

    async def oil_calibrate(call: ServiceCall) -> None:
        hub, dev = resolve(hass, call)
        usage, oil = hub.usage(dev), hub.oil(dev)
        for key in SETTABLE:
            if key in call.data:
                setattr(oil, key, call.data[key])
        problem = calibrate(oil, usage, call.data["action"], dt_util.utcnow().isoformat())
        hub.save_usage(dev, usage=usage, oil=oil)
        if problem:
            raise vol.Invalid(problem)

    async def send_frame(call: ServiceCall) -> None:
        """Diagnostics: send one inner frame (checksum computed); replies arrive as aroma_link_local_frame events."""
        hub, dev = resolve(hass, call)
        inner = codec.build_inner(dev.mac, int(call.data["cmd"], 16), int(call.data["sub"], 16),
                                  bytes.fromhex(call.data["payload"]))
        await hub.async_send(dev, inner)

    hass.services.async_register(DOMAIN, "send_frame", send_frame, SEND_FRAME_SCHEMA)
    hass.services.async_register(DOMAIN, "oil_refill", oil_refill, OIL_REFILL_SCHEMA)
    hass.services.async_register(DOMAIN, "oil_calibrate", oil_calibrate, OIL_CALIBRATE_SCHEMA)
    hass.services.async_register(DOMAIN, "start_timed_run", start_timed_run, START_RUN_SCHEMA)
    hass.services.async_register(DOMAIN, "cancel_timed_run", cancel_timed_run, SYNC_SCHEMA)
    hass.services.async_register(DOMAIN, "set_night_owl", set_night_owl, NIGHT_OWL_SCHEMA)
    hass.services.async_register(DOMAIN, "sync_schedules", sync_schedules, SYNC_SCHEMA)
    hass.services.async_register(DOMAIN, "set_schedule", set_schedule, SET_SCHEDULE_SCHEMA)
    hass.services.async_register(DOMAIN, "import_schedule", import_schedule, IMPORT_SCHEMA)
