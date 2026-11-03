"""GatingEngine: native replacement for the HVAC / occupancy / motion blueprints.

Evaluates on every watched-entity change, every minute and on device status reports, then
idempotently commands the device. Inert until a schedule was saved from HA, so a fresh install
never fights manual use.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from . import aroma_codec as codec
from .const import signal_uplink
from .gating import GateConfig, GateLogic, gating_plan, slots_fallback

_LOGGER = logging.getLogger(__name__)
EVALUATE_INTERVAL = timedelta(seconds=60)
COMMAND_SETTLE = timedelta(seconds=30)


class GatingEngine:
    def __init__(self, hass: HomeAssistant, hub, dev, config: GateConfig) -> None:
        self._hass, self._hub, self._dev = hass, hub, dev
        self.logic = GateLogic(config)
        self._unsubs: list = []
        self._last_cmd: tuple[bool, object] | None = None
        self._last_fan_at = None
        self._last_snapshot: dict | None = None
        self._off_since = None                      # when the gate last turned the device off
        self._fallback_warned = False
        self._fallback_active = False               # power holds the device off until the gate opens

    async def async_start(self) -> None:
        cfg = self.logic.config
        if cfg.watched_entities:
            self._unsubs.append(async_track_state_change_event(self._hass, cfg.watched_entities, self._on_state))
        self._unsubs.append(async_track_time_interval(self._hass, self._on_tick, EVALUATE_INTERVAL))
        if cfg.climate_entity:
            self._prime_hvac(self._hass.states.get(cfg.climate_entity))
        await self.async_evaluate("start")

    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    # ---------------------------------------------------------------- triggers

    @callback
    def _on_state(self, event) -> None:
        if event.data.get("entity_id") == self.logic.config.climate_entity:
            self._prime_hvac(event.data.get("new_state"))
        self._hass.async_create_task(self.async_evaluate("state_change"))

    @callback
    def _on_tick(self, _now) -> None:
        self._hass.async_create_task(self.async_evaluate("tick"))

    def _prime_hvac(self, state) -> None:
        self.logic.update_hvac(state.attributes.get("hvac_action") if state else None, dt_util.utcnow())

    # ---------------------------------------------------------------- inputs

    def _occupancy(self) -> bool | None:
        eid = self.logic.config.occupancy_entity
        state = self._hass.states.get(eid) if eid else None
        return None if state is None else state.state == "on"

    def _motion(self) -> list[tuple[str, object]]:
        out = []
        for eid in self.logic.config.motion_entities:
            state = self._hass.states.get(eid)
            if state is not None:
                out.append((state.state, state.last_changed))
        return out

    # ---------------------------------------------------------------- act

    def managed(self) -> bool:
        return self._hub.models.get(self._dev.topic_id).schedule.version > 0

    async def async_evaluate(self, reason: str = "tick") -> None:
        model = self._hub.models.get(self._dev.topic_id)
        rec = self._hub.reconciler(self._dev)
        timed = model.timed_run is not None
        if not (self.managed() or timed):
            return
        desired = self.logic.desired_power(
            model, dt_util.now(), dt_util.utcnow(), timed_run_active=timed,
            occupancy_on=self._occupancy(), motion=self._motion())
        decision = self.logic.snapshot.get("decision")
        power, slots_gated = gating_plan(self.logic.config.gating_mode, desired, decision)
        self.logic.snapshot.update(mode=self.logic.config.gating_mode, desired=desired)
        if slots_gated is not None and rec.gated_off != slots_gated:
            rec.gated_off = slots_gated
            rec.reset_budget()
            await rec.async_check()
        if self.logic.snapshot != self._last_snapshot:
            self._last_snapshot = dict(self.logic.snapshot)
            async_dispatcher_send(self._hass, signal_uplink(self._dev.topic_id), ("gate", self.logic.snapshot))
            self._hub.fire_update(self._dev.topic_id, "gating")
        now = dt_util.utcnow()
        if desired is False:
            self._off_since = self._off_since or now
        else:
            self._off_since = None
            self._fallback_active = False           # the gate opened: slots own the device again
        if self._fallback_active or slots_fallback(self.logic.config.gating_mode, desired,
                                                   (self._dev.status or {}).get("work_status"),
                                                   self._off_since, now):
            if not self._fallback_warned:
                _LOGGER.warning("%s: still diffusing with every slot disabled; slot gating cannot stop "
                                "this unit, switching it off by power (set gating mode to power to "
                                "make this the rule)", self._dev.topic_id)
            self._fallback_warned = True             # once per engine: the unit will not change
            self._fallback_active = True
            power = False
        if power is None:
            return
        await self._apply_power(power, reason)
        if bool(desired) and self.logic.config.keep_fan_on and self._dev.power:
            await self._ensure_fan_on(reason)

    async def _apply_power(self, want: bool, reason: str) -> None:
        if self._dev.power is None or bool(self._dev.power) == want:
            return
        now = dt_util.utcnow()
        if self._last_cmd and self._last_cmd[0] == want and now - self._last_cmd[1] < COMMAND_SETTLE:
            return      # a just-sent command may not be reflected yet
        _LOGGER.info("%s: gating turns the device %s (%s: %s)", self._dev.topic_id,
                     "on" if want else "off", reason, self.logic.snapshot.get("decision"))
        self._last_cmd = (want, now)
        await self._hub.async_send(self._dev, codec.power_frame(self._dev.mac, want))

    async def _ensure_fan_on(self, reason: str) -> None:
        if self._dev.fan:
            return
        now = dt_util.utcnow()
        if self._last_fan_at is not None and now - self._last_fan_at < COMMAND_SETTLE:
            return
        self._last_fan_at = now
        _LOGGER.info("%s: gating turns the fan on (%s)", self._dev.topic_id, reason)
        await self._hub.async_send(self._dev, codec.fan_frame(self._dev.mac, True))
