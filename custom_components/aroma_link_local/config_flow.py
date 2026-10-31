"""Config flow + options for Aroma-Link Local (single instance, auto-discovers)."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_DOWNLINK_PATTERN,
    CONF_QUERY_INTERVAL,
    CONF_UPLINK_WILDCARD,
    DEFAULT_DOWNLINK_PATTERN,
    DEFAULT_QUERY_INTERVAL,
    DEFAULT_UPLINK_WILDCARD,
    DOMAIN,
)


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

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        o = self._entry.options
        schema = vol.Schema({
            vol.Optional(
                CONF_QUERY_INTERVAL,
                default=o.get(CONF_QUERY_INTERVAL, DEFAULT_QUERY_INTERVAL),
            ): vol.All(int, vol.Range(min=0, max=3600)),
            vol.Optional(
                CONF_UPLINK_WILDCARD,
                default=o.get(CONF_UPLINK_WILDCARD, DEFAULT_UPLINK_WILDCARD),
            ): str,
            vol.Optional(
                CONF_DOWNLINK_PATTERN,
                default=o.get(CONF_DOWNLINK_PATTERN, DEFAULT_DOWNLINK_PATTERN),
            ): str,
        })
        return self.async_show_form(step_id="init", data_schema=schema)
