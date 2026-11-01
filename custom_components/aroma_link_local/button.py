"""Refill button: record a new oil bottle without resetting the calibration."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_NEW_DEVICE
from .entity import AromaEntity
from .usage import refill


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    hub = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _add(dev):
        if dev.topic_id not in known:
            known.add(dev.topic_id)
            async_add_entities([RefillButton(hass, hub, dev)])

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _add))
    for dev in list(hub.devices.values()):
        _add(dev)


class RefillButton(AromaEntity, ButtonEntity):
    _attr_icon = "mdi:water-plus-outline"

    def __init__(self, hass, hub, dev):
        super().__init__(hass, dev, "oil_refill_keep_calibration")
        self._hub = hub

    async def async_press(self) -> None:
        usage, oil = self._hub.usage(self._dev), self._hub.oil(self._dev)
        refill(oil, usage, dt_util.utcnow().isoformat(), keep_calibration=True)
        self._hub.save_usage(self._dev, usage=usage, oil=oil)
