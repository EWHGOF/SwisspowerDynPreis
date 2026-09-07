"""Smoke test: the integration can be set up inside Home Assistant."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.swisspower_dynpreis.const import (
    CONF_METHOD,
    CONF_TARIFF_NAME,
    CONF_TARIFF_TYPES,
    DOMAIN,
    METHOD_TARIFF_NAME,
)


async def test_setup_entry(hass: HomeAssistant) -> None:
    """The config entry loads and creates entities."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={
            CONF_NAME: "Test",
            CONF_METHOD: METHOD_TARIFF_NAME,
            CONF_TARIFF_NAME: "D1",
            CONF_TARIFF_TYPES: ["electricity"],
        },
        options={},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.swisspower_dynpreis.coordinator."
        "SwisspowerDynPreisApiClient.fetch_tariffs",
        return_value={"prices": []},
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert DOMAIN in hass.data
