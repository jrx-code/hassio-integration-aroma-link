"""Websocket API of the schedule card (``aroma_link_local/*``)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from . import cardapi
from .const import DOMAIN
from .model import NightOwlSettings, Weekday, WeeklySchedule
from .schedule import scheduled_work_seconds_per_day, validate_schedule
from .usage import days_remaining, level_percent, remaining_ml

WWW = Path(__file__).parent / "www"
CARD_FILE = "aroma-link-schedule-card.js"
ERR_UNKNOWN, ERR_CONFLICT, ERR_INVALID = "unknown_device", "version_conflict", "invalid_schedule"
ENTITIES = {  # card key -> (platform, unique_id suffix)
    "power": ("switch", "power"), "fan": ("switch", "fan"), "schedule_enabled": ("switch", "schedule_enabled"),
    "night_owl": ("switch", "night_owl"), "work_number": ("number", "work_duration"),
    "pause_number": ("number", "pause_duration"), "scheduled_on": ("binary_sensor", "scheduled_on"),
    "oil_level": ("sensor", "oil_level"), "oil_remaining": ("sensor", "oil_remaining"),
    "refill_button": ("button", "oil_refill_keep_calibration")}


def _find(hass: HomeAssistant, topic_id: str):
    for hub in hass.data.get(DOMAIN, {}).values():
        if hasattr(hub, "devices") and topic_id in hub.devices:
            return hub, hub.devices[topic_id]
    return None, None


def _sync(hub, dev) -> dict:
    rec = hub.reconciler(dev)
    managed = rec.managed()
    return cardapi.sync_state(managed, bool(dev.slots) and dev.slots == rec.desired(), rec.error)


def _schedule_payload(hub, dev) -> dict:
    m = hub.models.get(dev.topic_id)
    return {"schedule": m.schedule.to_dict(), "night_owl": m.night_owl.to_dict(),
            "flags": {"schedule_enabled": m.schedule_enabled, "night_owl_enabled": m.night_owl_enabled,
                      "default_work_sec": m.default_work_sec, "default_pause_sec": m.default_pause_sec},
            "sync": _sync(hub, dev)}


def _device(handler):
    """Resolve device_id; answer unknown_device otherwise."""
    async def wrapper(hass, connection, msg):
        hub, dev = _find(hass, str(msg["device_id"]))
        if dev is None:
            connection.send_error(msg["id"], ERR_UNKNOWN, "Unknown device_id")
            return
        await handler(hass, connection, msg, hub, dev)
    return wrapper


def _cmd(name: str, schema: dict):
    def deco(fn):
        return websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/{name}", vol.Required("device_id"): str, **schema})(
            websocket_api.async_response(_device(fn)))
    return deco


@_cmd("get_schedule", {})
async def ws_get_schedule(hass, connection, msg, hub, dev):
    connection.send_result(msg["id"], _schedule_payload(hub, dev))


@_cmd("save_schedule", {vol.Required("schedule"): dict, vol.Optional("night_owl"): dict, vol.Optional("base_version"): int})
async def ws_save_schedule(hass, connection, msg, hub, dev):
    model = hub.models.get(dev.topic_id)
    if msg.get("base_version") is not None and msg["base_version"] != model.schedule.version:
        connection.send_error(msg["id"], ERR_CONFLICT, f"Schedule changed elsewhere (have {model.schedule.version})")
        return
    try:
        schedule = WeeklySchedule.from_dict(msg["schedule"])
    except (KeyError, TypeError, ValueError) as err:
        connection.send_error(msg["id"], ERR_INVALID, f"Malformed schedule: {err}")
        return
    errors = validate_schedule(schedule)
    night_owl = None
    if "night_owl" in msg:
        errors += cardapi.validate_night_owl(msg["night_owl"])
        if not errors:
            night_owl = NightOwlSettings.from_dict({**model.night_owl.to_dict(), **msg["night_owl"]})
    if errors:
        connection.send_error(msg["id"], ERR_INVALID, "; ".join(errors))
        return
    schedule.version = model.schedule.version + 1
    schedule.updated_at = dt_util.utcnow().isoformat()
    model.schedule = schedule
    if night_owl is not None:
        model.night_owl = night_owl
    hub.models.changed(dev.topic_id, "schedule")
    connection.send_result(msg["id"], {"version": schedule.version, "sync": _sync(hub, dev)})


@_cmd("set_night_owl_days", {vol.Required("days"): [vol.All(int, vol.Range(min=0, max=6))], vol.Required("enabled"): bool})
async def ws_set_night_owl_days(hass, connection, msg, hub, dev):
    model = hub.models.get(dev.topic_id)
    for day in msg["days"]:
        model.schedule.days[Weekday(day)].night_owl = msg["enabled"]
    model.schedule.version += 1
    hub.models.changed(dev.topic_id, "schedule")
    connection.send_result(msg["id"], {"version": model.schedule.version})


@_cmd("set_flags", {vol.Optional("schedule_enabled"): bool, vol.Optional("night_owl_enabled"): bool})
async def ws_set_flags(hass, connection, msg, hub, dev):
    model = hub.models.get(dev.topic_id)
    for key in ("schedule_enabled", "night_owl_enabled"):
        if msg.get(key) is not None:
            setattr(model, key, msg[key])
    hub.models.changed(dev.topic_id, "flags")
    connection.send_result(msg["id"], {"version": model.schedule.version})


@_cmd("sync_now", {})
async def ws_sync_now(hass, connection, msg, hub, dev):
    rec = hub.reconciler(dev)
    rec.reset_budget()
    hass.async_create_task(rec.async_check(force=True))
    connection.send_result(msg["id"], {"sync": _sync(hub, dev)})


@_cmd("get_status", {})
async def ws_get_status(hass, connection, msg, hub, dev):
    model, usage, oil = hub.models.get(dev.topic_id), hub.usage(dev), hub.oil(dev)
    engine = hub.engines.get(dev.topic_id)
    now = dt_util.now()
    gating = dict(engine.logic.snapshot) if engine else {}
    gating["window"] = cardapi.window_payload(model, now)
    days = days_remaining(oil, usage.work_since_fill, scheduled_work_seconds_per_day(model, Weekday(now.weekday())))
    status = dev.status.get("work_status", "off") if dev.status else ("idle" if dev.power else "off")
    connection.send_result(msg["id"], {
        "power": bool(dev.power), "fan": bool(dev.fan), "work_status": cardapi.WORK_NUMBER.get(status, 0),
        "available": dev.last_seen is not None and (dt_util.utcnow() - dev.last_seen).total_seconds() < 600,
        "desired_power": gating.get("desired"), "gating": gating,
        "timed_run": cardapi.timed_run_payload(model.timed_run, dt_util.utcnow()),
        "oil": cardapi.oil_payload(oil, level_percent(oil, usage.work_since_fill), remaining_ml(oil, usage.work_since_fill),
                                   days, usage.work_since_fill),
        "sync": _sync(hub, dev)})


@_cmd("timed_run_start", {vol.Required("duration_minutes"): vol.All(int, vol.Range(min=1, max=1440)),
                          vol.Optional("work_sec"): vol.All(int, vol.Range(min=5, max=900)),
                          vol.Optional("pause_sec"): vol.All(int, vol.Range(min=5, max=900))})
async def ws_timed_run_start(hass, connection, msg, hub, dev):
    await hub.timed.async_start(dev, msg["duration_minutes"], msg.get("work_sec"), msg.get("pause_sec"))
    connection.send_result(msg["id"], {"timed_run": cardapi.timed_run_payload(hub.models.get(dev.topic_id).timed_run, dt_util.utcnow())})


@_cmd("timed_run_cancel", {})
async def ws_timed_run_cancel(hass, connection, msg, hub, dev):
    await hub.timed.async_cancel(dev)
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/list_devices"})
@websocket_api.async_response
async def ws_list_devices(hass, connection, msg):
    registry, devices = er.async_get(hass), []
    for hub in hass.data.get(DOMAIN, {}).values():
        for dev in getattr(hub, "devices", {}).values():
            mac_id = dev.mac.replace(":", "").lower()
            entities = {}
            for key, (platform, suffix) in ENTITIES.items():
                eid = registry.async_get_entity_id(platform, DOMAIN, f"aroma_{mac_id}_{suffix}")
                if eid:
                    entities[key] = eid
            devices.append({"device_id": dev.topic_id, "name": f"Aroma-Link {dev.mac}", "entities": entities})
    connection.send_result(msg["id"], {"devices": devices})


async def async_register(hass: HomeAssistant) -> None:
    """Websocket commands plus the card as an extra JS module (no Lovelace resource to manage)."""
    if hass.data.get(f"{DOMAIN}_ws_registered"):
        return
    hass.data[f"{DOMAIN}_ws_registered"] = True
    for fn in (ws_get_schedule, ws_save_schedule, ws_set_night_owl_days, ws_set_flags, ws_sync_now,
               ws_get_status, ws_timed_run_start, ws_timed_run_cancel, ws_list_devices):
        websocket_api.async_register_command(hass, fn)
    digest = hashlib.sha1(b"".join(p.read_bytes() for p in sorted(WWW.rglob("*.js")))).hexdigest()[:8]
    base = f"/aroma_link_local_card/{digest}"
    await hass.http.async_register_static_paths([StaticPathConfig(base, str(WWW), False)])
    add_extra_js_url(hass, f"{base}/{CARD_FILE}")
