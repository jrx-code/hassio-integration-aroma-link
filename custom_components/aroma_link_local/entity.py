"""Shared entity base: one device in the registry, uplink events via the dispatcher."""
from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity

from .const import DOMAIN, signal_uplink


def device_info(dev) -> DeviceInfo:
    mac_id = dev.mac.replace(":", "").lower()
    return DeviceInfo(
        identifiers={(DOMAIN, mac_id)},
        name=f"Aroma-Link {dev.mac}",
        manufacturer="DeWoo / Aroma-Link",
        model=dev.identity.get("model") or "Pro300",
        sw_version=dev.identity.get("firmware"),
        connections={("mac", dev.mac)},
    )


class AromaEntity(Entity):
    """Base for every entity: subclasses override ``_on_event`` to react to uplinks."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass, dev, key: str, name: str | None = None) -> None:
        self.hass = hass
        self._dev = dev
        self._attr_unique_id = f"aroma_{dev.mac.replace(':', '').lower()}_{key}"
        if name is not None:
            self._attr_name = name
        self._attr_translation_key = None if name is not None else key
        self._attr_device_info = device_info(dev)

    async def async_added_to_hass(self) -> None:
        @callback
        def _handle(ev) -> None:
            if self._on_event(ev):
                self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_uplink(self._dev.topic_id), _handle))

    @callback
    def _on_event(self, ev) -> bool:
        """Return True when the entity state changed and must be written."""
        return False
