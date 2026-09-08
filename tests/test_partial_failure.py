"""Tests for what happens when only some tariff types answer."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from aiohttp import ClientError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.swisspower_dynpreis.const import DOMAIN

from .helpers import (
    FakeApi,
    advance,
    at,
    day_slots,
    make_entry,
    set_time_zone,
    setup_integration,
)

TWO_TYPES = ["electricity", "grid"]
ELECTRICITY_PRICE = "sensor.test_electricity_current_price"
GRID_PRICE = "sensor.test_grid_current_price"


def _both_days(api: FakeApi) -> None:
    """Publish both days for both tariff types; grid is half of electricity."""
    for day, value in ((date(2026, 9, 7), 0.20), (date(2026, 9, 8), 0.30)):
        api.publish(day, day_slots(day, [value] * 24))
        api.publish(
            day,
            day_slots(day, [value / 2] * 24, tariff_type="grid"),
            tariff_type="grid",
        )


async def test_one_failing_type_does_not_take_the_others_down(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """The whole entry used to go unavailable when a single type failed.

    _async_fetch_all raised on the first failure, which discarded the payloads
    already fetched in that loop, and CoordinatorEntity.available is the single
    global last_update_success - so every entity of every type went unavailable
    over one broken tariff type.
    """
    await set_time_zone(hass)
    _both_days(api)

    freezer.move_to(at(2026, 9, 7, 5, 55))
    entry = make_entry(update_time="06:00", tariff_types=TWO_TYPES)
    await setup_integration(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    assert float(hass.states.get(ELECTRICITY_PRICE).state) == pytest.approx(0.20)
    assert float(hass.states.get(GRID_PRICE).state) == pytest.approx(0.10)

    # Only grid breaks.
    api.error = ClientError("grid is down")
    api.failing_types = {"grid"}
    await advance(hass, freezer, timedelta(minutes=20), step=timedelta(minutes=5))

    # electricity is untouched and fresh.
    assert float(hass.states.get(ELECTRICITY_PRICE).state) == pytest.approx(0.20)
    assert not coordinator.tariff_is_stale("electricity")

    # grid keeps its cached prices rather than going unavailable ...
    grid = hass.states.get(GRID_PRICE)
    assert grid.state not in ("unavailable", "unknown")
    assert float(grid.state) == pytest.approx(0.10)
    # ... and says so, so the outage is not invisible.
    assert coordinator.tariff_is_stale("grid")
    assert grid.attributes["from_cache"] is True
    assert hass.states.get(ELECTRICITY_PRICE).attributes["from_cache"] is False


async def test_a_partial_failure_still_schedules_a_retry(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Carrying data forward must not mean giving up on the broken type."""
    await set_time_zone(hass)
    _both_days(api)

    freezer.move_to(at(2026, 9, 7, 5, 55))
    entry = make_entry(update_time="06:00", tariff_types=TWO_TYPES)
    await setup_integration(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    api.error = ClientError("grid is down")
    api.failing_types = {"grid"}
    await advance(hass, freezer, timedelta(minutes=20), step=timedelta(minutes=5))
    assert coordinator.update_interval is not None, "the retry must be scheduled"

    # Once grid recovers, the retry picks it up and the flag clears.
    api.error = None
    api.failing_types = None
    await advance(hass, freezer, timedelta(minutes=40), step=timedelta(minutes=5))

    assert not coordinator.tariff_is_stale("grid")
    assert hass.states.get(GRID_PRICE).attributes["from_cache"] is False


async def test_last_successful_fetch_is_exposed_per_type(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """The attribute is what tells a user how old the prices really are."""
    await set_time_zone(hass)
    _both_days(api)

    freezer.move_to(at(2026, 9, 7, 5, 55))
    entry = make_entry(update_time="06:00", tariff_types=TWO_TYPES)
    await setup_integration(hass, entry)

    stamp = hass.states.get(GRID_PRICE).attributes["last_successful_fetch"]
    assert stamp is not None and stamp.startswith("2026-09-07T05:55")

    api.error = ClientError("grid is down")
    api.failing_types = {"grid"}
    await advance(hass, freezer, timedelta(minutes=20), step=timedelta(minutes=5))

    # Unchanged for grid: it has not succeeded since.
    assert hass.states.get(GRID_PRICE).attributes["last_successful_fetch"] == stamp
    # Moved on for electricity.
    assert (
        hass.states.get(ELECTRICITY_PRICE).attributes["last_successful_fetch"] != stamp
    )


async def test_a_total_outage_is_still_reported(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Carry-forward is per type, not a way to hide a complete outage.

    With no type answering and nothing cached, setup has to fail so Home
    Assistant retries it, rather than loading an entry that shows nothing.
    """
    await set_time_zone(hass)
    api.error = ClientError("everything is down")

    entry = make_entry(update_time="06:00", tariff_types=TWO_TYPES)
    entry.add_to_hass(hass)
    freezer.move_to(at(2026, 9, 7, 5, 55))

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_type_that_never_answered_is_unavailable(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """There is nothing to carry forward, so those entities must say so."""
    await set_time_zone(hass)
    _both_days(api)
    api.error = ClientError("grid has never worked")
    api.failing_types = {"grid"}

    freezer.move_to(at(2026, 9, 7, 5, 55))
    entry = make_entry(update_time="06:00", tariff_types=TWO_TYPES)
    await setup_integration(hass, entry)

    assert float(hass.states.get(ELECTRICITY_PRICE).state) == pytest.approx(0.20)
    assert hass.states.get(GRID_PRICE).state == "unavailable"
