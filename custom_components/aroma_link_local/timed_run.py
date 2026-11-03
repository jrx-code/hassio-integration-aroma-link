"""Restart-surviving 'run for N minutes'.

The end time lives in the model Store, so an HA restart mid-run re-arms the auto-off (or
settles the run at once when the end passed while HA was down). While a run is active slot 5
carries a 24/7 overlay so powering on diffuses regardless of schedule windows. Expiry clears
the overlay and the run, then hands power back (see ``timed_run_expiry_power``); cancel keeps
the device running and lets the schedule take over again.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from . import aroma_codec as codec
from .const import signal_uplink
from .model import TimedRun
from .schedule import overlay_slot, timed_run_expiry_power

_LOGGER = logging.getLogger(__name__)


class TimedRunManager:
    def __init__(self, hass: HomeAssistant, hub) -> None:
        self._hass, self._hub = hass, hub
        self._timers: dict[str, callable] = {}

    # ---------------------------------------------------------------- lifecycle

    async def async_restore(self) -> None:
        for dev in self._hub.devices.values():
            run = self._hub.models.get(dev.topic_id).timed_run
            if run is None:
                continue
            ends = dt_util.parse_datetime(run.ends_at)
            if ends is None or ends <= dt_util.utcnow():
                _LOGGER.info("%s: timed run expired while HA was down", dev.topic_id)
                await self._expire(dev)
            else:
                self._hub.reconciler(dev).overlay = overlay_slot(run.work_sec, run.pause_sec)
                self._arm(dev, ends)

    def stop(self) -> None:
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()

    def forget(self, dev) -> None:
        """Drop a removed device's expiry timer without commanding the device."""
        self._disarm(dev)

    # ---------------------------------------------------------------- public API

    async def async_start(self, dev, minutes: int, work_sec: int | None = None,
                          pause_sec: int | None = None) -> str:
        """Start (or replace) a run; returns the ISO end time."""
        self._disarm(dev)
        model = self._hub.models.get(dev.topic_id)
        prior = model.timed_run.prior_power if model.timed_run else dev.power
        work = int(work_sec or model.default_work_sec)
        pause = int(pause_sec or model.default_pause_sec)
        ends = dt_util.utcnow() + timedelta(minutes=int(minutes))
        model.timed_run = TimedRun(ends.isoformat(), work, pause, int(minutes), prior)
        rec = self._hub.reconciler(dev)
        rec.overlay = overlay_slot(work, pause)
        rec.reset_budget()
        self._hub.models.changed(dev.topic_id, "timed_run_state")     # persist, no schedule write yet
        await rec.async_check(force=True)
        if not dev.power:
            await self._hub.async_send(dev, codec.power_frame(dev.mac, True))
            dev.power = True
        self._arm(dev, ends)
        self._notify(dev)
        return model.timed_run.ends_at

    async def async_cancel(self, dev) -> None:
        """Clear the run but leave the device running; the schedule resumes ownership."""
        self._disarm(dev)
        await self._clear(dev)

    # ---------------------------------------------------------------- internals

    def _arm(self, dev, ends) -> None:
        self._disarm(dev)
        @callback
        def _fire(_now) -> None:
            self._hass.async_create_task(self._expire(dev))

        self._timers[dev.topic_id] = async_track_point_in_time(self._hass, _fire, ends)

    def _disarm(self, dev) -> None:
        cancel = self._timers.pop(dev.topic_id, None)
        if cancel:
            cancel()

    async def _clear(self, dev) -> None:
        model = self._hub.models.get(dev.topic_id)
        model.timed_run = None
        rec = self._hub.reconciler(dev)
        rec.overlay = None
        rec.reset_budget()
        self._hub.models.changed(dev.topic_id, "timed_run_state")
        await rec.async_check(force=True)
        self._notify(dev)

    async def _expire(self, dev) -> None:
        model = self._hub.models.get(dev.topic_id)
        run = model.timed_run
        await self._clear(dev)
        want = timed_run_expiry_power(model.schedule_enabled, run.prior_power if run else None)
        if want is not None:
            # always send: after a restart dev.power is the restored value, not the device's truth
            await self._hub.async_send(dev, codec.power_frame(dev.mac, want))
            dev.power = want
        _LOGGER.info("%s: timed run finished (power command: %s)", dev.topic_id, want)

    def _notify(self, dev) -> None:
        async_dispatcher_send(self._hass, signal_uplink(dev.topic_id), ("timed_run", None))
