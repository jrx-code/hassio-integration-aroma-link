"""Default work / pause durations (used for timed runs and new schedule windows)."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_NEW_DEVICE
from .entity import AromaEntity
from .model import PAUSE_SEC_MAX, PAUSE_SEC_MIN, WORK_SEC_MAX, WORK_SEC_MIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    hub = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _add(dev):
        if dev.topic_id in known:
            return
        known.add(dev.topic_id)
        async_add_entities([
            DefaultDuration(hass, hub, dev, "work_duration", "default_work_sec", "mdi:spray",
                            WORK_SEC_MIN, WORK_SEC_MAX, 1),
            DefaultDuration(hass, hub, dev, "pause_duration", "default_pause_sec", "mdi:timer-pause",
                            PAUSE_SEC_MIN, PAUSE_SEC_MAX, 5),
        ])

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _add))
    for dev in list(hub.devices.values()):
        _add(dev)


class DefaultDuration(AromaEntity, NumberEntity):
    _attr_native_unit_of_measurement = "s"
    _attr_mode = NumberMode.BOX

    def __init__(self, hass, hub, dev, key, field, icon, lo, hi, step):
        super().__init__(hass, dev, key)
        self._hub = hub
        self._field = field
        self._attr_icon = icon
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        self._attr_native_step = step

    @property
    def native_value(self):
        return getattr(self._hub.models.get(self._dev.topic_id), self._field)

    async def async_set_native_value(self, value: float) -> None:
        setattr(self._hub.models.get(self._dev.topic_id), self._field, int(value))
        self._hub.models.changed(self._dev.topic_id, "defaults")

    @callback
    def _on_event(self, ev) -> bool:
        return ev[0] == "model"
