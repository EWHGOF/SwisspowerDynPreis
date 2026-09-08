"""Tests for the update/refresh behaviour of the Swisspower DynPreis integration.

These tests pin down four behaviours:

1. Entity state follows the price curve during the day, without re-fetching.
2. Tomorrow's prices are picked up once the API publishes them.
3. A failed refresh is retried on the same day.
4. Changing options takes effect without a Home Assistant restart, and
   unloading the entry leaves no timers behind.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from aiohttp import ClientError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.swisspower_dynpreis.const import CONF_UPDATE_TIME, DOMAIN

from .helpers import (
    AVG_TODAY,
    AVG_TOMORROW,
    CURRENT_PRICE,
    FakeApi,
    advance,
    at,
    day_slots,
    hourly,
    make_entry,
    set_time_zone,
    setup_integration,
)


async def test_current_price_follows_the_curve_without_refetching(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Problem 1: the price sensor must track the slot the clock is in."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, hourly(today))

    freezer.move_to(at(2026, 9, 7, 6, 30))
    await setup_integration(hass, make_entry())

    assert hass.states.get(CURRENT_PRICE).state == "0.16"
    calls_after_setup = api.call_count

    # Cross into the 07:00 slot. The data is already cached, so no new request
    # may be made - but the state must change.
    await advance(hass, freezer, timedelta(hours=1))

    assert hass.states.get(CURRENT_PRICE).state == "0.17"
    assert (
        api.call_count == calls_after_setup
    ), "crossing a slot boundary must not trigger an API request"


async def test_current_price_follows_quarter_hourly_slots(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Boundaries come from the data, so 15-minute slots must work too."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(
        today,
        day_slots(
            today, [round(0.10 + i * 0.001, 4) for i in range(96)], slot_minutes=15
        ),
    )

    freezer.move_to(at(2026, 9, 7, 6))
    await setup_integration(hass, make_entry())

    # 06:00 is slot 24 -> 0.10 + 24 * 0.001
    assert hass.states.get(CURRENT_PRICE).state == "0.124"

    await advance(hass, freezer, timedelta(minutes=15), step=timedelta(minutes=1))

    assert hass.states.get(CURRENT_PRICE).state == "0.125"


async def test_midnight_rollover_repartitions_today_and_tomorrow(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Problem 1: at local midnight, yesterday's 'tomorrow' becomes 'today'."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    tomorrow = date(2026, 9, 8)
    api.publish(today, day_slots(today, [0.20] * 24))
    api.publish(tomorrow, day_slots(tomorrow, [0.30] * 24))

    freezer.move_to(at(2026, 9, 7, 23, 30))
    await setup_integration(hass, make_entry())

    assert float(hass.states.get(AVG_TODAY).state) == pytest.approx(0.20)
    assert float(hass.states.get(AVG_TOMORROW).state) == pytest.approx(0.30)

    await advance(hass, freezer, timedelta(hours=1))

    assert float(hass.states.get(AVG_TODAY).state) == pytest.approx(
        0.30
    ), "after midnight the cached next-day curve must become 'today'"


async def test_tomorrow_is_fetched_once_the_api_publishes_it(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Problem 2: a morning-only fetch can never see tomorrow's prices."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    tomorrow = date(2026, 9, 8)
    api.publish(today, day_slots(today, [0.20] * 24))

    # Start after the configured fetch time, so the daily trigger cannot fire
    # right after setup and make this test pass for the wrong reason.
    freezer.move_to(at(2026, 9, 7, 7))
    await setup_integration(hass, make_entry(update_time="06:00"))

    assert hass.states.get(AVG_TOMORROW).state in ("unknown", "unavailable")
    calls_after_setup = api.call_count

    # The API publishes tomorrow's curve in the early afternoon.
    api.publish(tomorrow, day_slots(tomorrow, [0.30] * 24))
    await advance(hass, freezer, timedelta(hours=10), step=timedelta(minutes=15))

    assert api.call_count > calls_after_setup, (
        "the integration must query again during the day to look for tomorrow"
    )

    assert float(hass.states.get(AVG_TOMORROW).state) == pytest.approx(
        0.30
    ), "the integration must re-query once tomorrow's prices are published"


async def test_failed_refresh_is_retried_the_same_day(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Problem 3: a failed fetch must not park the integration until tomorrow."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    # Set up just before the morning anchor. async_track_time_change searches
    # from utcnow + 1s, so an anchor the clock already sits on would not fire
    # at all today.
    freezer.move_to(at(2026, 9, 7, 5, 55))
    await setup_integration(hass, make_entry(update_time="06:00"))
    assert float(hass.states.get(AVG_TODAY).state) == pytest.approx(0.20)

    # The API breaks, then the 06:00 anchor fires and fails.
    api.error = ClientError("boom")
    await advance(hass, freezer, timedelta(minutes=20), step=timedelta(minutes=5))
    calls_after_failure = api.call_count
    assert calls_after_failure > 1, "the anchor must have fired and failed"

    # The API recovers. The backoff must retry within the hour, not tomorrow.
    api.error = None
    await advance(hass, freezer, timedelta(minutes=40), step=timedelta(minutes=5))

    assert (
        api.call_count > calls_after_failure
    ), "a failed refresh must be retried without waiting for the next day"
    assert float(hass.states.get(AVG_TODAY).state) == pytest.approx(0.20)


async def test_options_change_takes_effect_without_restart(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Problem 4: changing the fetch time must reconfigure the running entry."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    entry = make_entry(update_time="06:00")
    freezer.move_to(at(2026, 9, 7, 8))
    await setup_integration(hass, entry)
    calls_before = api.call_count

    hass.config_entries.async_update_entry(entry, options={CONF_UPDATE_TIME: "09:00"})
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert (
        api.call_count > calls_before
    ), "an options change must reload the entry so the new fetch time applies"

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert (
        coordinator._update_time.hour == 9
    ), "the running coordinator must pick up the new fetch time"


async def test_unload_leaves_no_timers_behind(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Problem 4: teardown must cancel every scheduled callback."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    entry = make_entry()
    freezer.move_to(at(2026, 9, 7, 6))
    await setup_integration(hass, entry)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    calls_after_unload = api.call_count

    await advance(hass, freezer, timedelta(hours=26), step=timedelta(minutes=30))

    assert (
        api.call_count == calls_after_unload
    ), "an unloaded entry must not keep firing timers"


async def test_slot_without_timestamp_does_not_break_rendering(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A slot the API sent without timestamps must be skipped, not crash."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    slots = [
        {"electricity": [{"component": "energy", "unit": "CHF/kWh", "value": 9.9}]},
        *day_slots(today, [0.20] * 24),
    ]
    api.publish(today, slots)

    freezer.move_to(at(2026, 9, 7, 6))
    await setup_integration(hass, make_entry())

    # find_current_slot walks the list in order, so the bad slot is reached
    # before the matching one; normalize_price_slots walks all of them.
    state = hass.states.get(CURRENT_PRICE)
    assert state is not None
    assert float(state.state) == pytest.approx(0.20)
    assert float(hass.states.get(AVG_TODAY).state) == pytest.approx(0.20)
