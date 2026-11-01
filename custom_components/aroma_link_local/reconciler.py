"""Keeps the device's 5 slots equal to what the model compiles for today.

Triggers: model change, a schedule dump from the device (drift), midnight (new weekday),
start-up, and the ``sync_schedules`` service. Writes are verified by the next dump; a device
that keeps rejecting a write is left alone after 3 attempts in 5 minutes.
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import replace

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import aroma_codec as codec
from .model import NIGHT_OWL_SLOT_INDEX, today
from .schedule import compile_day

_LOGGER = logging.getLogger(__name__)
MAX_WRITES = 3
WINDOW_SEC = 300
VERIFY_DELAY = 3


class Reconciler:
    def __init__(self, hass: HomeAssistant, hub, dev) -> None:
        self._hass, self._hub, self._dev = hass, hub, dev
        self.overlay: codec.Slot | None = None     # timed run (set by the timed-run manager)
        self.gated_off = False                      # schedule-flip gating (gating engine)
        self._writes: list[float] = []
        self.error: str | None = None

    def desired(self) -> list[codec.Slot]:
        model = self._hub.models.get(self._dev.topic_id)
        if model.schedule.version == 0 and self._dev.slots:
            # no schedule saved from HA yet: keep the device's own windows, only apply overlay/gate
            slots = list(self._dev.slots)
            if self.gated_off:
                slots = [replace(s, enabled=False) for s in slots]
            if self.overlay is not None:
                slots[NIGHT_OWL_SLOT_INDEX] = self.overlay
            return slots
        return compile_day(model, today(dt_util.now()), self.overlay, self.gated_off)

    def managed(self) -> bool:
        """Only a device with a schedule saved from HA is written; fresh installs leave it alone."""
        return self._hub.models.get(self._dev.topic_id).schedule.version > 0 or self.overlay is not None

    async def async_check(self, force: bool = False) -> bool:
        """Write the desired slots when the device differs. Returns True when a write was sent."""
        if not force and not self.managed():
            return False
        if not self._dev.slots and not force:
            await self._hub.async_send(self._dev, codec.query_frame(self._dev.mac))   # learn first
            return False
        desired = self.desired()
        if self._dev.slots == desired:
            return False
        now = _time.monotonic()
        self._writes = [t for t in self._writes if now - t < WINDOW_SEC]
        if len(self._writes) >= MAX_WRITES and not force:
            _LOGGER.error("%s: slots keep differing after %d writes, giving up until the next change",
                          self._dev.topic_id, MAX_WRITES)
            self.error = "device keeps rejecting the schedule"
            self._hub.fire_update(self._dev.topic_id, "sync")
            return False
        self._writes.append(now)
        _LOGGER.info("%s: writing schedule slots for today", self._dev.topic_id)
        self._hub.fire_update(self._dev.topic_id, "sync")
        await self._hub.async_send(self._dev, codec.schedule_frame(self._dev.mac, desired))
        self._hass.loop.call_later(VERIFY_DELAY, lambda: self._hass.async_create_task(
            self._hub.async_send(self._dev, codec.query_frame(self._dev.mac))))
        return True

    def reset_budget(self) -> None:
        self._writes.clear()
        self.error = None
