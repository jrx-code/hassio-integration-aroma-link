"""Default work / pause durations (used for timed runs and new schedule windows)."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
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
            BottleCapacity(hass, hub, dev),
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


class BottleCapacity(AromaEntity, NumberEntity):
    """Oil bottle size in ml (Pro300: 300; other models 400 or 1000). The Refill button fills to this."""

    _attr_native_unit_of_measurement = "ml"
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:bottle-tonic-outline"
    _attr_native_min_value = 50
    _attr_native_max_value = 2000
    _attr_native_step = 10
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass, hub, dev):
        super().__init__(hass, dev, "bottle_capacity")
        self._hub = hub

    @property
    def native_value(self):
        return self._hub.oil(self._dev).bottle_capacity

    async def async_set_native_value(self, value: float) -> None:
        oil = self._hub.oil(self._dev)
        oil.bottle_capacity = float(value)
        self._hub.save_usage(self._dev, oil=oil)

    @callback
    def _on_event(self, ev) -> bool:
        return ev[0] in ("model", "identity")
