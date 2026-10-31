"""Aroma-Link Local — auto-discovering local control of ANY Aroma-Link device.

One config entry. Subscribes the MQTT uplink wildcard on the broker HA's MQTT
integration uses (EMQX). Every device that publishes is discovered: the device
topic-id from the topic, the MAC from the inner frame. No per-device MAC, no
cloud. Options: periodic state poll, topic patterns. See docs/cutover.md.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from . import aroma_codec as codec
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

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["switch"]


@dataclass
class AromaDevice:
    topic_id: str          # device id from the topic, e.g. qdpu118T<maclower>
    mac: str               # AA:BB:CC:DD:EE:FF (from the inner frame)
    downlink_pattern: str = DEFAULT_DOWNLINK_PATTERN
    env: codec.Envelope = field(default_factory=codec.Envelope)

    @property
    def t_down(self) -> str:
        return self.downlink_pattern.format(id=self.topic_id)


class AromaHub:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.devices: dict[str, AromaDevice] = {}
        o = entry.options
        self._wildcard = o.get(CONF_UPLINK_WILDCARD, DEFAULT_UPLINK_WILDCARD)
        self._downlink = o.get(CONF_DOWNLINK_PATTERN, DEFAULT_DOWNLINK_PATTERN)
        self._query_interval = int(o.get(CONF_QUERY_INTERVAL, DEFAULT_QUERY_INTERVAL))

    async def async_start(self, entry: ConfigEntry) -> None:
        entry.async_on_unload(
            await mqtt.async_subscribe(self.hass, self._wildcard, self._on_uplink, encoding=None))
        if self._query_interval > 0:
            entry.async_on_unload(async_track_time_interval(
                self.hass, self._poll, timedelta(seconds=self._query_interval)))

    @callback
    def _poll(self, _now) -> None:
        for dev in self.devices.values():
            payload = dev.env.wrap(codec.query_frame(dev.mac), tag=codec.TAG_DOWN)
            self.hass.async_create_task(
                mqtt.async_publish(self.hass, dev.t_down, payload))

    @callback
    def _on_uplink(self, msg) -> None:
        parts = msg.topic.split("/")          # q/2/d/<topic_id>/bus
        if len(parts) < 4:
            return
        topic_id = parts[3]
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
        ev = codec.interpret_uplink(inner)
        if ev:
            async_dispatcher_send(self.hass, signal_uplink(topic_id), ev)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub = AromaHub(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await hub.async_start(entry)
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return ok
