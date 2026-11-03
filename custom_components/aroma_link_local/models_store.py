"""Persisted per-device desired state (schedule, flags, defaults), in the HA Store."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import DOMAIN, signal_uplink
from .model import DeviceModel

SAVE_DELAY = 2


class ModelStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store = Store(hass, 1, f"{DOMAIN}.models")
        self._models: dict[str, DeviceModel] = {}
        self.on_change = None   # callable(topic_id, change), set by the hub

    async def async_load(self) -> None:
        raw = await self._store.async_load() or {}
        self._models = {t: DeviceModel.from_dict(d) for t, d in raw.items()}

    def get(self, topic_id: str) -> DeviceModel:
        """The device's model (a default one is created on first access)."""
        return self._models.setdefault(topic_id, DeviceModel())

    def changed(self, topic_id: str, change: str) -> None:
        """Persist after the caller mutated the model, and tell the entities."""
        self._store.async_delay_save(self._snapshot, SAVE_DELAY)
        async_dispatcher_send(self._hass, signal_uplink(topic_id), ("model", change))
        if self.on_change is not None:
            self.on_change(topic_id, change)

    def remove(self, topic_id: str) -> None:
        """Forget a removed device's model and write the store at once."""
        if self._models.pop(topic_id, None) is not None:
            self._store.async_delay_save(self._snapshot, 0)

    def _snapshot(self) -> dict:
        return {t: m.to_dict() for t, m in self._models.items()}

    def flush(self) -> None:
        self._store.async_delay_save(self._snapshot, 0)
