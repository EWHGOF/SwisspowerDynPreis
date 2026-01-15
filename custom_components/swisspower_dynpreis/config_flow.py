"""Config flow for Swisspower DynPreis."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers import config_validation as cv

from .const import (
    API_BASE,
    CONF_API_URL,
    CONF_METERING_CODE,
    CONF_METHOD,
    CONF_TARIFF_NAME,
    CONF_TARIFF_TYPES,
    CONF_TOKEN,
    CONF_UPDATE_INTERVAL,
    DEFAULT_NAME,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    METHOD_METERING_CODE,
    METHOD_TARIFF_NAME,
    TARIFF_TYPES,
)


class SwisspowerDynPreisConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Swisspower DynPreis."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            self._method = user_input[CONF_METHOD]
            self._name = user_input.get(CONF_NAME, DEFAULT_NAME)
            self._api_url = user_input[CONF_API_URL]

            if self._method == METHOD_METERING_CODE:
                return await self.async_step_metering()
            return await self.async_step_tariff_name()

        schema = vol.Schema(
            {
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_API_URL, default=API_BASE): cv.url,
                vol.Required(CONF_METHOD, default=METHOD_METERING_CODE): vol.In(
                    {
                        METHOD_METERING_CODE: "Messpunktnummer",
                        METHOD_TARIFF_NAME: "Tarifname",
                    }
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_metering(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            return self.async_create_entry(
                title=self._name,
                data={
                    CONF_NAME: self._name,
                    CONF_METHOD: METHOD_METERING_CODE,
                    CONF_API_URL: self._api_url,
                    CONF_METERING_CODE: user_input[CONF_METERING_CODE],
                    CONF_TOKEN: user_input[CONF_TOKEN],
                    CONF_TARIFF_TYPES: user_input[CONF_TARIFF_TYPES],
                },
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_METERING_CODE): str,
                vol.Required(CONF_TOKEN): str,
                vol.Required(CONF_TARIFF_TYPES, default=TARIFF_TYPES): cv.multi_select(TARIFF_TYPES),
            }
        )
        return self.async_show_form(step_id="metering", data_schema=schema, errors=errors)

    async def async_step_tariff_name(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            return self.async_create_entry(
                title=self._name,
                data={
                    CONF_NAME: self._name,
                    CONF_METHOD: METHOD_TARIFF_NAME,
                    CONF_API_URL: self._api_url,
                    CONF_TARIFF_NAME: user_input[CONF_TARIFF_NAME],
                    CONF_TARIFF_TYPES: user_input[CONF_TARIFF_TYPES],
                },
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_TARIFF_NAME): str,
                vol.Required(CONF_TARIFF_TYPES, default=TARIFF_TYPES): cv.multi_select(TARIFF_TYPES),
            }
        )
        return self.async_show_form(step_id="tariff_name", data_schema=schema, errors=errors)

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return SwisspowerDynPreisOptionsFlowHandler(config_entry)


class SwisspowerDynPreisOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an options flow for Swisspower DynPreis."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_UPDATE_INTERVAL,
                    default=self._config_entry.options.get(
                        CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=1440)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
