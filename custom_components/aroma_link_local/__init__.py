"""Aroma-Link Local, auto-discovering local control of ANY Aroma-Link device.

One config entry. Subscribes the MQTT uplink wildcard on the broker HA's MQTT
integration uses (EMQX). Every device that publishes is discovered: the device
topic-id from the topic, the MAC from the inner frame. No per-device MAC, no
cloud. Options: periodic state poll, topic patterns. See docs/cutover.md.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import aroma_codec as codec
from .cardapi import CHANGE_FOR_MODEL
from .const import (
    CONF_DOWNLINK_PATTERN,
    CONF_QUERY_INTERVAL,
    CONF_UPLINK_WILDCARD,
    DEFAULT_DOWNLINK_PATTERN,
    DEFAULT_QUERY_INTERVAL,
    DEFAULT_UPLINK_WILDCARD,
    DOMAIN,
    SIGNAL_NEW_DEVICE,
    signal_uplink,
)
from .engine import GatingEngine
from .gating import GateConfig
from .model import topic_ids_for
from .models_store import ModelStore
from .reconciler import Reconciler
from .schedule import wall_clock_drift
from .services import async_register_services
from .timed_run import TimedRunManager
from .usage import Oil, Usage, capacity_from_model, sample
from .ws_api import async_register as async_register_ws

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["switch", "sensor", "number", "binary_sensor", "button"]
SAVE_DELAY = 10   # seconds; coalesces the device-state writes


@dataclass
class AromaDevice:
    topic_id: str          # device id from the topic, e.g. qdpu118T<maclower>
    mac: str               # AA:BB:CC:DD:EE:FF (from the inner frame)
    downlink_pattern: str = DEFAULT_DOWNLINK_PATTERN
    env: codec.Envelope = field(default_factory=codec.Envelope)
    # Last known device state, restored from the Store after a restart.
    power: bool | None = None
    fan: bool | None = None
    last_seen: datetime | None = None
    identity: dict = field(default_factory=dict)    # firmware, model (53/F1)
    sys_info: dict = field(default_factory=dict)    # module, app, mcu ... (/sys)
    slots: list = field(default_factory=list)       # last schedule dump (52/15)
    status: dict = field(default_factory=dict)      # last 52/25 report (not persisted)
    status_at: datetime | None = None

    def clock_drift(self) -> float | None:
        """Seconds the device clock is ahead of HA at its last report (it reports local time)."""
        if not self.status or self.status_at is None:
            return None
        return wall_clock_drift(self.status["clock"], dt_util.as_local(self.status_at))

    @property
    def t_down(self) -> str:
        return self.downlink_pattern.format(id=self.topic_id)

    def to_store(self) -> dict:
        return {
            "mac": self.mac,
            "power": self.power,
            "fan": self.fan,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "identity": self.identity,
            "sys": self.sys_info,
            "slots": [vars(s) for s in self.slots],
        }

    @classmethod
    def from_store(cls, topic_id: str, raw, downlink_pattern: str) -> AromaDevice:
        if isinstance(raw, str):                    # store v1 held only the MAC
            raw = {"mac": raw}
        last_seen = dt_util.parse_datetime(raw["last_seen"]) if raw.get("last_seen") else None
        return cls(
            topic_id=topic_id, mac=raw["mac"], downlink_pattern=downlink_pattern,
            power=raw.get("power"), fan=raw.get("fan"), last_seen=last_seen,
            identity=raw.get("identity") or {}, sys_info=raw.get("sys") or {},
            slots=[codec.Slot(**s) for s in raw.get("slots") or []],
        )


class AromaHub:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.devices: dict[str, AromaDevice] = {}
        o = entry.options
        self._wildcard = o.get(CONF_UPLINK_WILDCARD, DEFAULT_UPLINK_WILDCARD)
        self._downlink = o.get(CONF_DOWNLINK_PATTERN, DEFAULT_DOWNLINK_PATTERN)
        self._query_interval = int(o.get(CONF_QUERY_INTERVAL, DEFAULT_QUERY_INTERVAL))
        # Discovered devices persist across restarts (a Store, not entry.data, so
        # saving does not fire the update listener and reload the entry).
        self._store = Store(hass, 1, f"{DOMAIN}.devices")
        self.models = ModelStore(hass)
        self.models.on_change = self._model_changed
        self.reconcilers: dict[str, Reconciler] = {}
        self.timed = TimedRunManager(hass, self)
        self.engines: dict[str, GatingEngine] = {}
        self._options = entry.options

    async def async_start(self, entry: ConfigEntry) -> None:
        await self.models.async_load()
        entry.async_on_unload(
            await mqtt.async_subscribe(self.hass, self._wildcard, self._on_uplink, encoding=None))
        # Restore devices seen before: without this, after a restart nothing is
        # polled and nothing is discovered until the device happens to publish.
        for topic_id, raw in ((await self._store.async_load()) or {}).items():
            if topic_id not in self.devices:
                dev = AromaDevice.from_store(topic_id, raw, self._downlink)
                self.devices[topic_id] = dev
                async_dispatcher_send(self.hass, SIGNAL_NEW_DEVICE, dev)
        await self.timed.async_restore()
        for dev in list(self.devices.values()):
            self._start_engine(dev)
        if self.devices:
            self._poll(None)   # ask for state right away, do not wait an interval
        if self._query_interval > 0:
            entry.async_on_unload(async_track_time_interval(
                self.hass, self._poll, timedelta(seconds=self._query_interval)))
        entry.async_on_unload(self._flush)
        entry.async_on_unload(self.models.flush)
        entry.async_on_unload(self.timed.stop)
        entry.async_on_unload(self._stop_engines)
        entry.async_on_unload(async_track_time_change(self.hass, self._midnight, hour=0, minute=0, second=15))

    def reconciler(self, dev: AromaDevice) -> Reconciler:
        if dev.topic_id not in self.reconcilers:
            self.reconcilers[dev.topic_id] = Reconciler(self.hass, self, dev)
        return self.reconcilers[dev.topic_id]

    def _start_engine(self, dev: AromaDevice) -> None:
        if dev.topic_id not in self.engines:
            engine = GatingEngine(self.hass, self, dev, GateConfig.from_options(self._options, dev.topic_id))
            self.engines[dev.topic_id] = engine
            self.hass.async_create_task(engine.async_start())

    @callback
    def _stop_engines(self) -> None:
        for engine in self.engines.values():
            engine.stop()
        self.engines.clear()

    @callback
    def _model_changed(self, topic_id: str, change: str) -> None:
        dev = self.devices.get(topic_id)
        if change in CHANGE_FOR_MODEL:
            self.fire_update(topic_id, CHANGE_FOR_MODEL[change])
        if dev is not None and change in ("schedule", "flags", "night_owl", "timed_run_state"):
            if change != "timed_run_state":     # the timed-run manager writes its own overlay
                rec = self.reconciler(dev)
                rec.reset_budget()
                self.hass.async_create_task(rec.async_check())
            engine = self.engines.get(topic_id)
            if engine is not None:
                self.hass.async_create_task(engine.async_evaluate("model"))

    @callback
    def _midnight(self, _now) -> None:
        """A new weekday: the device needs the new day's slots."""
        for dev in self.devices.values():
            rec = self.reconciler(dev)
            rec.reset_budget()
            self.hass.async_create_task(rec.async_check())

    @callback
    def remove_device(self, topic_id: str) -> None:
        """Forget a device removed from Home Assistant. Nothing is sent: it keeps running as it was.

        It is no longer polled; if it publishes again (e.g. after a power cycle) it is discovered
        as a new device with default settings.
        """
        dev = self.devices.pop(topic_id, None)
        engine = self.engines.pop(topic_id, None)
        if engine is not None:
            engine.stop()
        if dev is not None:
            self.timed.forget(dev)
        self.reconcilers.pop(topic_id, None)
        self.models.remove(topic_id)
        self._flush()
        _LOGGER.info("removed Aroma-Link device %s", topic_id)

    # ------------------------------------------------------------------ io

    @callback
    def _flush(self) -> None:
        """Write the device snapshot now (called on unload)."""
        self._store.async_delay_save(self._snapshot, 0)

    def _snapshot(self) -> dict:
        return {t: d.to_store() for t, d in self.devices.items()}

    def _save_soon(self) -> None:
        self._store.async_delay_save(self._snapshot, SAVE_DELAY)

    async def async_send(self, dev: AromaDevice, inner: bytes) -> None:
        """Wrap an inner frame in a fresh envelope and publish it on the downlink."""
        await mqtt.async_publish(self.hass, dev.t_down, dev.env.wrap(inner, tag=codec.TAG_DOWN))

    @callback
    def _poll(self, _now) -> None:
        for dev in self.devices.values():
            self.hass.async_create_task(self._query(dev))

    async def _query(self, dev: AromaDevice) -> None:
        await self.async_send(dev, codec.status_frame(dev.mac))
        await self.async_send(dev, codec.query_frame(dev.mac))

    # ------------------------------------------------------------- uplink

    @callback
    def _on_uplink(self, msg) -> None:
        parts = msg.topic.split("/")          # q/2/d/<topic_id>/bus|sys
        if len(parts) < 5:
            return
        topic_id, channel = parts[3], parts[4]
        now = dt_util.utcnow()
        if channel == "sys":
            dev = self.devices.get(topic_id)
            info = codec.parse_sys(msg.payload)
            if dev is not None:
                self.hass.bus.async_fire(f"{DOMAIN}_frame", {"device": topic_id, "channel": "sys", "hex": bytes(msg.payload).hex().upper()})
            if dev is not None and info is None:
                dev.sys_info["boot_binary"] = bytes(msg.payload).hex().upper()    # unidentified fields, kept for analysis
                self._save_soon()
            if dev is not None and info:
                dev.sys_info.update(info)
                dev.last_seen = now
                self._save_soon()
                async_dispatcher_send(self.hass, signal_uplink(topic_id), ("sys", info))
            return
        inner = codec.unwrap(msg.payload)
        if not inner or not codec.parse_inner(inner):
            return
        dev = self.devices.get(topic_id)
        if dev is None:
            mac = codec.mac_from_inner(inner) or topic_id[-12:]
            dev = AromaDevice(topic_id=topic_id, mac=mac, downlink_pattern=self._downlink)
            self.devices[topic_id] = dev
            _LOGGER.info("discovered Aroma-Link device %s (mac %s)", topic_id, dev.mac)
            async_dispatcher_send(self.hass, SIGNAL_NEW_DEVICE, dev)
            self._start_engine(dev)
        dev.last_seen = now
        parsed = codec.parse_inner(inner)
        self.hass.bus.async_fire(f"{DOMAIN}_frame", {"device": topic_id, "channel": "bus", "hex": inner.hex().upper(),
                                                     "cmd": f"{parsed.cmd:02X}", "sub": f"{parsed.sub:02X}"})
        ev = codec.interpret_uplink(inner)
        if ev:
            self._apply(dev, ev)
            async_dispatcher_send(self.hass, signal_uplink(topic_id), ev)
        else:
            async_dispatcher_send(self.hass, signal_uplink(topic_id), ("seen", now))
        self._save_soon()

    def _apply(self, dev: AromaDevice, ev: tuple) -> None:
        kind, value = ev
        if kind == "power":
            dev.power = value
        elif kind == "fan":
            dev.fan = value
        elif kind == "schedule":
            dev.slots = value
            self.fire_update(dev.topic_id, "sync")
            self.hass.async_create_task(self.reconciler(dev).async_check())   # drift check
        elif kind == "status":
            dev.status, dev.status_at = value, dt_util.utcnow()
            dev.power = value["power"]          # reconciles changes made on the device itself
            self._count_usage(dev, value)
            engine = self.engines.get(dev.topic_id)
            if engine is not None:
                self.hass.async_create_task(engine.async_evaluate("status"))
        elif kind == "identity":
            dev.identity = value
            self._update_registry(dev)

    def fire_update(self, topic_id: str, change: str) -> None:
        """The single bus event the schedule card listens to."""
        self.hass.bus.async_fire(f"{DOMAIN}_updated", {"device_id": topic_id, "change": change, "version": None})

    def usage(self, dev: AromaDevice) -> Usage:
        return Usage.from_dict(self.models.get(dev.topic_id).usage)

    def oil(self, dev: AromaDevice) -> Oil:
        raw = self.models.get(dev.topic_id).oil
        oil = Oil.from_dict(raw)
        if "bottle_capacity" not in raw:          # never set: take the model's size when the announce told us
            cap = capacity_from_model(dev.identity.get("model"))
            if cap:
                oil.bottle_capacity = oil.fill_volume = cap
        return oil

    def save_usage(self, dev: AromaDevice, usage: Usage | None = None, oil: Oil | None = None) -> None:
        model = self.models.get(dev.topic_id)
        if usage is not None:
            model.usage = usage.to_dict()
        if oil is not None:
            model.oil = oil.to_dict()
        self.models.changed(dev.topic_id, "usage")

    def _count_usage(self, dev: AromaDevice, status: dict) -> None:
        model = self.models.get(dev.topic_id)
        slot = codec.active_slot(dev.slots, dt_util.now().hour * 60 + dt_util.now().minute)
        work = slot.work_sec if slot else model.default_work_sec
        pause = slot.pause_sec if slot else model.default_pause_sec
        usage = self.usage(dev)
        if sample(usage, dt_util.utcnow().timestamp(), status["power"], status["work_status"], work, pause):
            self.save_usage(dev, usage=usage)

    def _update_registry(self, dev: AromaDevice) -> None:
        reg = dr.async_get(self.hass)
        mac_id = dev.mac.replace(":", "").lower()
        entry = reg.async_get_device(identifiers={(DOMAIN, mac_id)})
        if entry is not None:
            reg.async_update_device(
                entry.id, sw_version=dev.identity.get("firmware"),
                model=dev.identity.get("model") or entry.model)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub = AromaHub(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await hub.async_start(entry)
    async_register_services(hass)
    await async_register_ws(hass)
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(hass: HomeAssistant, entry: ConfigEntry,
                                           device_entry: dr.DeviceEntry) -> bool:
    """Allow Delete on the device page; the hub forgets the device."""
    hub = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if hub is not None:
        macs = {topic_id: dev.mac for topic_id, dev in hub.devices.items()}
        for topic_id in topic_ids_for(macs, device_entry.identifiers, DOMAIN):
            hub.remove_device(topic_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return ok
