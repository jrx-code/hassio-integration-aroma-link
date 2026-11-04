"""Config flow + options for Aroma-Link Local (single instance, auto-discovers)."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_DOWNLINK_PATTERN,
    CONF_QUERY_INTERVAL,
    CONF_UPLINK_WILDCARD,
    DEFAULT_DOWNLINK_PATTERN,
    DEFAULT_QUERY_INTERVAL,
    DEFAULT_UPLINK_WILDCARD,
    DOMAIN,
)
from .gating import GATING_MODES, GateConfig

NUMBER_MIN = selector.NumberSelector(selector.NumberSelectorConfig(
    min=0, max=240, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="min"))
THRESHOLD = selector.NumberSelector(selector.NumberSelectorConfig(
    min=0, max=100000, step="any", mode=selector.NumberSelectorMode.BOX))


class AromaLinkLocalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """One entry; devices appear automatically from MQTT uplinks."""

    VERSION = 2

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Aroma-Link Local", data={})
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return AromaLinkOptionsFlow(config_entry)


class AromaLinkOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self._entry = config_entry
        self._device: str | None = None

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(step_id="init", menu_options=["settings", "gates"])

    async def async_step_settings(self, user_input=None):
        o = self._entry.options
        if user_input is not None:
            return self.async_create_entry(title="", data={**o, **user_input})
        schema = vol.Schema({
            vol.Optional(CONF_QUERY_INTERVAL, default=o.get(CONF_QUERY_INTERVAL, DEFAULT_QUERY_INTERVAL)):
                vol.All(int, vol.Range(min=0, max=3600)),
            vol.Optional(CONF_UPLINK_WILDCARD, default=o.get(CONF_UPLINK_WILDCARD, DEFAULT_UPLINK_WILDCARD)): str,
            vol.Optional(CONF_DOWNLINK_PATTERN, default=o.get(CONF_DOWNLINK_PATTERN, DEFAULT_DOWNLINK_PATTERN)): str,
        })
        return self.async_show_form(step_id="settings", data_schema=schema)

    async def async_step_gates(self, user_input=None):
        """Pick the device whose gates are edited (devices appear once they have published)."""
        hub = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        devices = {d.topic_id: d.mac for d in (hub.devices.values() if hub else [])}
        if not devices:
            return self.async_abort(reason="no_devices")
        if user_input is not None:
            self._device = user_input["device"]
            return await self.async_step_gate_config()
        schema = vol.Schema({vol.Required("device", default=next(iter(devices))): vol.In(
            {tid: f"Aroma-Link {mac}" for tid, mac in devices.items()})})
        return self.async_show_form(step_id="gates", data_schema=schema)

    async def async_step_gate_config(self, user_input=None):
        topic_id = self._device
        cur = GateConfig.from_options(self._entry.options, topic_id)
        if user_input is not None:
            gates = {**(self._entry.options.get("gates") or {}), topic_id: {
                k: v for k, v in user_input.items() if v not in (None, "", [])}}
            gates[topic_id].setdefault("keep_fan_on", False)
            return self.async_create_entry(title="", data={**self._entry.options, "gates": gates})

        def opt(key, value):
            return vol.Optional(key, description={"suggested_value": value} if value not in (None, [], "") else {})

        schema = vol.Schema({
            opt("climate_entity", cur.climate_entity): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["climate", "sensor"])),
            opt("hvac_start_threshold", cur.hvac_start_threshold): THRESHOLD,
            opt("hvac_stop_threshold", cur.hvac_stop_threshold): THRESHOLD,
            opt("occupancy_entity", cur.occupancy_entity): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["binary_sensor", "input_boolean", "group"])),
            opt("motion_entities", cur.motion_entities): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["binary_sensor", "input_boolean"], multiple=True)),
            vol.Optional("hvac_on_delay_minutes", default=cur.hvac_on_delay_minutes): NUMBER_MIN,
            vol.Optional("hvac_off_delay_minutes", default=cur.hvac_off_delay_minutes): NUMBER_MIN,
            vol.Optional("occupancy_off_delay_minutes", default=cur.occupancy_off_delay_minutes): NUMBER_MIN,
            vol.Optional("gating_mode", default=cur.gating_mode): selector.SelectSelector(
                selector.SelectSelectorConfig(options=list(GATING_MODES), translation_key="gating_mode")),
            vol.Optional("keep_fan_on", default=cur.keep_fan_on): bool,
            opt("block_entities", cur.block_entities): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["binary_sensor", "input_boolean"], multiple=True)),
            vol.Optional("min_off_minutes", default=cur.min_off_minutes): NUMBER_MIN,
            vol.Optional("max_on_minutes", default=cur.max_on_minutes): NUMBER_MIN,
            vol.Optional("rest_minutes", default=cur.rest_minutes): NUMBER_MIN,
        })
        return self.async_show_form(step_id="gate_config", data_schema=schema)
