"""Scheduled On: powered AND inside an armed window, Night Owl period or timed run."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_NEW_DEVICE
from .entity import AromaEntity
from .schedule import capability


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    hub = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _add(dev):
        if dev.topic_id not in known:
            known.add(dev.topic_id)
            async_add_entities([ScheduledOn(hass, hub, dev)])

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _add))
    for dev in list(hub.devices.values()):
        _add(dev)


class ScheduledOn(AromaEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass, hub, dev):
        super().__init__(hass, dev, "scheduled_on")
        self._hub = hub

    def _source(self) -> str | None:
        rec = self._hub.reconcilers.get(self._dev.topic_id)
        return capability(self._hub.models.get(self._dev.topic_id), dt_util.now(),
                          overlay_active=bool(rec and rec.overlay is not None),
                          gated_off=bool(rec and rec.gated_off))

    @property
    def is_on(self) -> bool:
        return bool(self._dev.power) and self._source() is not None

    @property
    def extra_state_attributes(self):
        engine = self._hub.engines.get(self._dev.topic_id)
        return {"source": self._source(), "gating": engine.logic.snapshot if engine else None}

    @callback
    def _on_event(self, ev) -> bool:
        return ev[0] in ("power", "status", "model", "timed_run", "gate")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _tick(_now) -> None:       # window boundaries emit no event
            self.async_write_ha_state()

        self.async_on_remove(async_track_time_interval(self.hass, _tick, timedelta(minutes=1)))
