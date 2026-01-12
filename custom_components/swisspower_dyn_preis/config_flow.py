"""Config flow for Swisspower dynamic price integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_METERING_CODE,
    CONF_TARIFF_NAME,
    CONF_TARIFF_TYPE,
    CONF_TOKEN,
    DEFAULT_TARIFF_TYPE,
    DOMAIN,
    TARIFF_TYPES,
)


class SwisspowerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Swisspower dynamic price."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            metering_code = user_input.get(CONF_METERING_CODE)
            tariff_name = user_input.get(CONF_TARIFF_NAME)

            if bool(metering_code) == bool(tariff_name):
                errors["base"] = "select_metering_or_tariff"
            else:
                return self.async_create_entry(
                    title=metering_code or tariff_name,
                    data=user_input,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_TOKEN): str,
                vol.Optional(CONF_METERING_CODE): str,
                vol.Optional(CONF_TARIFF_NAME): vol.In(["D0", "D1", "D2", "D3"]),
                vol.Optional(CONF_TARIFF_TYPE, default=DEFAULT_TARIFF_TYPE): vol.In(
                    TARIFF_TYPES
                ),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
