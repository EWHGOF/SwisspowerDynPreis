"""Config flow for Swisspower DynPreis."""

from __future__ import annotations

from datetime import time
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .const import (
    API_BASE,
    CONF_API_URL,
    CONF_METERING_CODE,
    CONF_METHOD,
    CONF_TARIFF_NAME,
    CONF_TARIFF_TYPES,
    CONF_TOKEN,
    CONF_UPDATE_TIME,
    CONF_UPDATE_TIME_PM,
    CONF_UPDATE_TIME_EVENING,
    CONF_QUERY_YEAR,
    CONF_VAT_ENTITY,
    CONF_VAT_RATE,
    CONF_VAT_TARIFF_TYPES,
    DEFAULT_NAME,
    DEFAULT_UPDATE_TIME,
    DEFAULT_UPDATE_TIME_PM,
    DEFAULT_UPDATE_TIME_EVENING,
    DEFAULT_VAT_RATE,
    DOMAIN,
    MAX_VAT_RATE,
    METHOD_METERING_CODE,
    METHOD_TARIFF_NAME,
    TARIFF_TYPES,
    VAT_EXEMPT_TARIFF_TYPES,
)
from .vat import parse_vat_rate

# The daily fetch times, in the order the forms show them.
UPDATE_TIME_KEYS = (
    (CONF_UPDATE_TIME, DEFAULT_UPDATE_TIME),
    (CONF_UPDATE_TIME_PM, DEFAULT_UPDATE_TIME_PM),
    (CONF_UPDATE_TIME_EVENING, DEFAULT_UPDATE_TIME_EVENING),
)


def _time_default(value: str) -> str:
    """Return a time the way the TimeSelector hands it back."""
    return dt_util.parse_time(value).strftime("%H:%M:%S")


class SwisspowerDynPreisConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Swisspower DynPreis."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            self._method = user_input[CONF_METHOD]
            self._name = user_input.get(CONF_NAME, DEFAULT_NAME)
            try:
                self._api_url = cv.url(user_input[CONF_API_URL])
            except vol.Invalid:
                errors[CONF_API_URL] = "invalid_url"
            else:
                if self._method == METHOD_METERING_CODE:
                    return await self.async_step_metering()
                return await self.async_step_tariff_name()

        schema = vol.Schema(
            {
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,

                vol.Required(CONF_API_URL, default=API_BASE): str,

                vol.Required(CONF_METHOD, default=METHOD_METERING_CODE): vol.In(
                    {
                        METHOD_METERING_CODE: "Authentifizierungstoken (Messpunktnummer)",
                        METHOD_TARIFF_NAME: "Tarifname (ohne Token)",
                    }
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_metering(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data = {
                CONF_NAME: self._name,
                CONF_METHOD: METHOD_METERING_CODE,
                CONF_API_URL: self._api_url,
                CONF_METERING_CODE: user_input[CONF_METERING_CODE],
                CONF_TOKEN: user_input[CONF_TOKEN],
                CONF_TARIFF_TYPES: user_input[CONF_TARIFF_TYPES],
            }
            return await self.async_step_schedule()

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
            self._data = {
                CONF_NAME: self._name,
                CONF_METHOD: METHOD_TARIFF_NAME,
                CONF_API_URL: self._api_url,
                CONF_TARIFF_NAME: user_input[CONF_TARIFF_NAME],
                CONF_TARIFF_TYPES: user_input[CONF_TARIFF_TYPES],
            }
            return await self.async_step_schedule()

        schema = vol.Schema(
            {
                vol.Required(CONF_TARIFF_NAME): str,
                vol.Required(CONF_TARIFF_TYPES, default=TARIFF_TYPES): cv.multi_select(TARIFF_TYPES),
            }
        )
        return self.async_show_form(step_id="tariff_name", data_schema=schema, errors=errors)

    async def async_step_schedule(self, user_input: dict[str, Any] | None = None):
        """Ask for the daily fetch times.

        They go into the options rather than the data: the coordinator reads
        them from there, and the options flow edits them there later on.
        """
        if user_input is not None:
            return self.async_create_entry(
                title=self._name,
                data=self._data,
                options={key: user_input[key] for key, _ in UPDATE_TIME_KEYS},
            )

        schema = vol.Schema(
            {
                vol.Required(key, default=_time_default(default)): selector.TimeSelector()
                for key, default in UPDATE_TIME_KEYS
            }
        )
        return self.async_show_form(step_id="schedule", data_schema=schema)

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

        query_year = self._config_entry.options.get(CONF_QUERY_YEAR)
        default_year = "" if query_year in (None, "") else str(query_year)

        default_vat_rate = parse_vat_rate(self._config_entry.options.get(CONF_VAT_RATE))
        if default_vat_rate is None:
            default_vat_rate = DEFAULT_VAT_RATE
        # No ``default`` for the entity: voluptuous fills a default in when the
        # key is missing, and the key is exactly what is missing after the user
        # clears the field - so a default would make the entity impossible to
        # remove again. A suggested value pre-fills the form without that.
        vat_entity = self._config_entry.options.get(CONF_VAT_ENTITY) or None
        default_vat_types = self._default_vat_tariff_types()

        # Every key the coordinator reads has to be in this schema: saving the
        # options replaces the whole dict, so a missing key gets dropped.
        schema = vol.Schema(
            {
                **{
                    vol.Optional(
                        key,
                        default=self._option_time(key, default).strftime("%H:%M:%S"),
                    ): selector.TimeSelector()
                    for key, default in UPDATE_TIME_KEYS
                },
                vol.Optional(
                    CONF_VAT_RATE,
                    default=default_vat_rate,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=MAX_VAT_RATE,
                        # The rate is typed, not dragged: a slider cannot hit
                        # 8.1 and "any" keeps voluptuous from rejecting it for
                        # not being a multiple of some step.
                        step="any",
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="%",
                    )
                ),
                vol.Optional(
                    CONF_VAT_ENTITY,
                    description={"suggested_value": vat_entity},
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain=["input_number", "number", "sensor"]
                    )
                ),
                vol.Required(
                    CONF_VAT_TARIFF_TYPES,
                    default=default_vat_types,
                ): cv.multi_select(self._configured_tariff_types()),
                vol.Optional(
                    CONF_QUERY_YEAR,
                    default=default_year,
                ): selector.TextSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    def _configured_tariff_types(self) -> list[str]:
        """Return the tariff types this entry was set up for."""
        tariff_types = self._config_entry.data.get(CONF_TARIFF_TYPES)
        if not isinstance(tariff_types, list):
            return []
        return [
            tariff_type
            for tariff_type in TARIFF_TYPES
            if tariff_type in tariff_types
        ]

    def _default_vat_tariff_types(self) -> list[str]:
        """Return which tariff types the VAT checkboxes start out ticked for.

        A stored list is a choice the user made and is shown back as it is,
        minus any type the entry no longer fetches. Nothing stored means the
        option was never touched, and then everything but the exempt types is
        ticked - adding VAT to a feed-in credit would be a wrong number, so it
        has to be asked for rather than arrived at by default.
        """
        configured = self._configured_tariff_types()
        stored = self._config_entry.options.get(CONF_VAT_TARIFF_TYPES)
        if isinstance(stored, list):
            return [
                tariff_type for tariff_type in configured if tariff_type in stored
            ]
        return [
            tariff_type
            for tariff_type in configured
            if tariff_type not in VAT_EXEMPT_TARIFF_TYPES
        ]

    def _option_time(self, key: str, fallback: str) -> time:
        """Read a stored time option, falling back to its default."""
        value = self._config_entry.options.get(key, fallback)
        if isinstance(value, str):
            value = dt_util.parse_time(value)
        if not isinstance(value, time):
            value = dt_util.parse_time(fallback)
        return value
