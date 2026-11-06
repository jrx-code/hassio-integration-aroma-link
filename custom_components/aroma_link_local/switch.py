"""Power + Fan switches, created per auto-discovered device."""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import aroma_codec as codec
from .const import DOMAIN, SIGNAL_NEW_DEVICE
from .entity import AromaEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    hub = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _add(dev):
        if dev.topic_id in known:
            return
        known.add(dev.topic_id)
        async_add_entities([
            AromaSwitch(hass, hub, dev, "power", None, codec.SUB_POWER, "mdi:spray"),
            AromaSwitch(hass, hub, dev, "fan", None, codec.SUB_FAN, "mdi:fan"),
            FlagSwitch(hass, hub, dev, "schedule_enabled", "schedule_enabled", "mdi:calendar-check"),
            FlagSwitch(hass, hub, dev, "night_owl", "night_owl_enabled", "mdi:owl"),
        ])

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _add))
    # devices discovered before this platform finished loading
    for dev in list(hub.devices.values()):
        _add(dev)


class AromaSwitch(AromaEntity, SwitchEntity):
    def __init__(self, hass, hub, dev, key, name, sub, icon):
        super().__init__(hass, dev, key, name)
        self._hub = hub
        self._key = key
        self._sub = sub
        self._attr_icon = icon

    @property
    def is_on(self) -> bool:
        return bool(getattr(self._dev, self._key))   # restored state; None counts as off

    @callback
    def _on_event(self, ev) -> bool:
        return ev[0] == self._key

    async def _send(self, on: bool):
        await self._hub.async_send(self._dev, codec.switch_frame(self._dev.mac, self._sub, on))
        setattr(self._dev, self._key, on)        # optimistic until the 53/1x state frame lands
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        await self._send(True)

    async def async_turn_off(self, **kwargs):
        await self._send(False)


class FlagSwitch(AromaEntity, SwitchEntity):
    """A persisted master flag of the device model (Schedule enabled, Night Owl)."""

    def __init__(self, hass, hub, dev, key, flag, icon):
        super().__init__(hass, dev, key)
        self._hub = hub
        self._flag = flag
        self._attr_icon = icon

    @property
    def is_on(self) -> bool:
        return getattr(self._hub.models.get(self._dev.topic_id), self._flag)

    @callback
    def _on_event(self, ev) -> bool:
        return ev[0] == "model"

    async def _set(self, value: bool) -> None:
        setattr(self._hub.models.get(self._dev.topic_id), self._flag, value)
        self._hub.models.changed(self._dev.topic_id, "flags")

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)
