"""Smoke test: the integration can be set up inside Home Assistant."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.swisspower_dynpreis.const import DOMAIN

from .helpers import FakeApi, make_entry, set_time_zone, setup_integration


async def test_setup_entry(hass: HomeAssistant, api: FakeApi) -> None:
    """The config entry loads and the coordinator is stored in hass.data."""
    await set_time_zone(hass)
    entry = make_entry()
    await setup_integration(hass, entry)

    assert DOMAIN in hass.data
    assert entry.entry_id in hass.data[DOMAIN]
    assert api.call_count == 1
