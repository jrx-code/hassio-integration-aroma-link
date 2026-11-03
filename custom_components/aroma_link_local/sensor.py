"""Device information sensors: firmware, last update, work status."""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from . import aroma_codec as codec
from .const import DOMAIN, SIGNAL_NEW_DEVICE
from .entity import AromaEntity
from .model import today
from .schedule import scheduled_work_seconds_per_day
from .usage import (
    days_remaining,
    effective_rate,
    level_percent,
    rate_source,
    remaining_ml,
)

WORK_STATES = ["off", "diffusing", "paused", "idle"]


def work_status(dev, now: datetime) -> str:
    """The device's own report (52/25); off/idle estimate from power + slots until the first report."""
    if dev.status:
        return dev.status["work_status"]
    if not dev.power:
        return "off"
    return "diffusing" if codec.active_slot(dev.slots, now.hour * 60 + now.minute) else "idle"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    hub = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _add(dev):
        if dev.topic_id in known:
            return
        known.add(dev.topic_id)
        async_add_entities([
            FirmwareSensor(hass, dev),
            LastUpdateSensor(hass, dev),
            WorkStatusSensor(hass, dev),
            RemainSensor(hass, dev, "work_remain", "mdi:spray"),
            RemainSensor(hass, dev, "pause_remain", "mdi:timer-pause"),
            DeviceClockSensor(hass, dev),
            ClockDriftSensor(hass, dev),
            UsageSensor(hass, hub, dev, "cumulative_runtime", "mdi:timer-sand"),
            UsageSensor(hass, hub, dev, "on_count", "mdi:counter"),
            UsageSensor(hass, hub, dev, "pump_count", "mdi:pump"),
            OilSensor(hass, hub, dev, "oil_level", "mdi:bottle-tonic-outline", "%"),
            OilSensor(hass, hub, dev, "oil_remaining", "mdi:water", "ml"),
        ])

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _add))
    for dev in list(hub.devices.values()):
        _add(dev)


class FirmwareSensor(AromaEntity, SensorEntity):
    _attr_icon = "mdi:chip"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass, dev):
        super().__init__(hass, dev, "firmware_version")

    @property
    def native_value(self):
        return self._dev.identity.get("firmware")

    @property
    def extra_state_attributes(self):
        return {"model": self._dev.identity.get("model"), **self._dev.sys_info}   # includes boot_binary (unidentified)

    @callback
    def _on_event(self, ev) -> bool:
        return ev[0] in ("identity", "sys")


class LastUpdateSensor(AromaEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass, dev):
        super().__init__(hass, dev, "last_update")

    @property
    def native_value(self):
        return self._dev.last_seen

    @callback
    def _on_event(self, ev) -> bool:
        return True   # every uplink moves last_seen


class WorkStatusSensor(AromaEntity, SensorEntity):
    _attr_icon = "mdi:state-machine"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = WORK_STATES

    def __init__(self, hass, dev):
        super().__init__(hass, dev, "work_status")

    @property
    def native_value(self):
        return work_status(self._dev, dt_util.now())

    @callback
    def _on_event(self, ev) -> bool:
        return ev[0] in ("power", "schedule", "status")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _tick(_now) -> None:       # window boundaries emit no event
            self.async_write_ha_state()

        self.async_on_remove(async_track_time_interval(self.hass, _tick, timedelta(minutes=1)))


class RemainSensor(AromaEntity, SensorEntity):
    """Seconds left in the current work or pause phase, from the 52/25 report."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = "s"

    def __init__(self, hass, dev, key, icon):
        super().__init__(hass, dev, key)
        self._key = key
        self._attr_icon = icon

    @property
    def native_value(self):
        return self._dev.status.get(self._key) if self._dev.status else None

    @callback
    def _on_event(self, ev) -> bool:
        return ev[0] == "status"


def device_clock(dev):
    """The device's clock as an aware datetime (it reports local time), or None."""
    if not dev.status:
        return None
    return dev.status["clock"].replace(tzinfo=dt_util.get_default_time_zone())


class DeviceClockSensor(AromaEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass, dev):
        super().__init__(hass, dev, "device_clock")

    @property
    def native_value(self):
        return device_clock(self._dev)

    @callback
    def _on_event(self, ev) -> bool:
        return ev[0] == "status"


class ClockDriftSensor(AromaEntity, SensorEntity):
    """Device clock minus HA clock at the time of the report; a drifting clock shifts schedules."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = "s"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass, dev):
        super().__init__(hass, dev, "clock_drift")

    @property
    def native_value(self):
        clock = device_clock(self._dev)
        if clock is None or self._dev.status_at is None:
            return None
        return round((clock - self._dev.status_at).total_seconds())

    @callback
    def _on_event(self, ev) -> bool:
        return ev[0] == "status"


class UsageSensor(AromaEntity, SensorEntity):
    """Estimated counters (see usage.py): runtime of the pump, power-on count, work cycles."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, hass, hub, dev, key, icon):
        super().__init__(hass, dev, key)
        self._hub, self._key, self._attr_icon = hub, key, icon
        if key == "cumulative_runtime":
            self._attr_device_class = SensorDeviceClass.DURATION
            self._attr_native_unit_of_measurement = "s"

    @property
    def native_value(self):
        u = self._hub.usage(self._dev)
        return {"cumulative_runtime": round(u.work_seconds, 1), "on_count": u.on_count,
                "pump_count": u.pump_count}[self._key]

    @property
    def extra_state_attributes(self):
        if self._key != "cumulative_runtime":
            return {"estimated": True}
        u = self._hub.usage(self._dev)
        return {"estimated": True, "runtime_hours": round(u.work_seconds / 3600, 3),
                "completed_cycles": round(u.cycles, 1), "work_seconds_since_fill": round(u.work_since_fill, 1)}

    @callback
    def _on_event(self, ev) -> bool:
        return ev[0] == "model"


class OilSensor(AromaEntity, SensorEntity):
    """Oil level (%) and remaining volume (ml): calibrated usage rate, else the nominal default."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hass, hub, dev, key, icon, unit):
        super().__init__(hass, dev, key)
        self._hub, self._key = hub, key
        self._attr_icon, self._attr_native_unit_of_measurement = icon, unit

    @property
    def native_value(self):
        oil, u = self._hub.oil(self._dev), self._hub.usage(self._dev)
        value = level_percent(oil, u.work_since_fill) if self._key == "oil_level" else remaining_ml(oil, u.work_since_fill)
        return None if value is None else round(value, 1)

    @property
    def extra_state_attributes(self):
        oil, u = self._hub.oil(self._dev), self._hub.usage(self._dev)
        model = self._hub.models.get(self._dev.topic_id)
        days = days_remaining(oil, u.work_since_fill, scheduled_work_seconds_per_day(model, today(dt_util.now())))
        return {
            "calibrated": oil.usage_rate is not None, "calibration_state": oil.calibration_state,
            "calibration_method": oil.calibration_method, "bottle_capacity_ml": oil.bottle_capacity,
            "fill_volume_ml": oil.fill_volume, "fill_date": oil.fill_date,
            "usage_rate_ml_per_hour": round(oil.usage_rate * 3600, 4) if oil.usage_rate else None,
            "rate_source": rate_source(oil), "estimate_rate_ml_per_hour": round(effective_rate(oil) * 3600, 4),
            "work_hours_since_fill": round(u.work_since_fill / 3600, 3),
            "estimated_days_remaining_schedule": None if days is None else round(days, 1),
            "recent_events": oil.events[-5:],
        }

    @callback
    def _on_event(self, ev) -> bool:
        return ev[0] == "model"
