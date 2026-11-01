"""Power + Fan switches, created per auto-discovered device."""
from __future__ import annotations

import logging

from homeassistant.components import mqtt
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo

from . import AromaDevice
from . import aroma_codec as codec
from .const import DOMAIN, SIGNAL_NEW_DEVICE, signal_uplink

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    hub = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _add(dev: AromaDevice):
        if dev.topic_id in known:
            return
        known.add(dev.topic_id)
        async_add_entities([
            AromaSwitch(hass, dev, "power", "Power", codec.SUB_POWER, "mdi:spray"),
            AromaSwitch(hass, dev, "fan", "Fan", codec.SUB_FAN, "mdi:fan"),
        ])

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _add))
    # devices discovered before this platform finished loading
    for dev in list(hub.devices.values()):
        _add(dev)


class AromaSwitch(SwitchEntity):
    _attr_has_entity_name = True

    def __init__(self, hass, dev: AromaDevice, key, name, sub, icon):
        self.hass = hass
        self._dev = dev
        self._sub = sub
        self._attr_name = name
        self._attr_icon = icon
        mac_id = dev.mac.replace(":", "").lower()
        self._attr_unique_id = f"aroma_{mac_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac_id)},
            name=f"Aroma-Link {dev.mac}",
            manufacturer="DeWoo / Aroma-Link",
            model="Pro300",
            connections={("mac", dev.mac)},
        )
        self._attr_is_on = False

    async def async_added_to_hass(self):
        @callback
        def _on_uplink(ev):
            if ev[0] == "fan" and self._sub == codec.SUB_FAN:
                self._attr_is_on = ev[1]
                self.async_write_ha_state()
            # power has no dedicated state uplink -> optimistic
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_uplink(self._dev.topic_id), _on_uplink))

    async def _send(self, on: bool):
        inner = codec.switch_frame(self._dev.mac, self._sub, on)
        payload = self._dev.env.wrap(inner, tag=codec.TAG_DOWN)
        await mqtt.async_publish(self.hass, self._dev.t_down, payload)
        self._attr_is_on = on
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        await self._send(True)

    async def async_turn_off(self, **kwargs):
        await self._send(False)
