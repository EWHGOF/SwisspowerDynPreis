"""Tests for the awkward cases: DST changes, repeated reloads, teardown."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.swisspower_dynpreis.const import DOMAIN

from .helpers import (
    CURRENT_PRICE,
    FakeApi,
    advance,
    at,
    day_slots,
    make_entry,
    set_time_zone,
    setup_integration,
    slots_from,
)


async def test_render_survives_dst_spring_forward(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """On 2026-03-29 Europe/Zurich skips 02:00-03:00; rendering must not.

    The day has 23 hourly slots. Slot 1 runs 01:00-01:59 at +01:00 and slot 2
    starts at 03:00 at +02:00 - one hour of real time later, with no wall-clock
    02:00 in between. A wall-clock timer pattern would either miss or repeat
    that boundary; an absolute instant cannot.
    """
    await set_time_zone(hass)
    # Absolute instants, so the slots are what a real API would send.
    api.publish(
        date(2026, 3, 29),
        slots_from(at(2026, 3, 29, 0, 0), [round(i * 0.01, 4) for i in range(23)]),
    )

    freezer.move_to(at(2026, 3, 29, 1, 30))
    await setup_integration(hass, make_entry(update_time="06:00"))

    assert hass.states.get(CURRENT_PRICE).state == "0.01"
    calls_after_setup = api.call_count

    # 01:30+01:00 to 03:30+02:00 is one hour of real time, across the gap.
    freezer.move_to(at(2026, 3, 29, 3, 30))
    await advance(hass, freezer, timedelta(minutes=5), step=timedelta(minutes=1))

    assert hass.states.get(CURRENT_PRICE).state == "0.02", (
        "the slot after the spring-forward gap must be picked up"
    )
    assert api.call_count == calls_after_setup, "no fetch is needed to re-render"


async def test_render_survives_dst_fall_back(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """On 2026-10-25 Europe/Zurich repeats 02:00-03:00; 25 slots, no double fire."""
    await set_time_zone(hass)
    api.publish(
        date(2026, 10, 25),
        slots_from(at(2026, 10, 25, 0, 0), [round(i * 0.01, 4) for i in range(25)]),
    )

    freezer.move_to(at(2026, 10, 25, 1, 30))
    await setup_integration(hass, make_entry(update_time="06:00"))
    assert hass.states.get(CURRENT_PRICE).state == "0.01"
    calls_after_setup = api.call_count

    # Walk through the repeated hour in real time and out the other side.
    await advance(hass, freezer, timedelta(hours=4), step=timedelta(minutes=10))

    state = hass.states.get(CURRENT_PRICE).state
    assert state not in ("unknown", "unavailable"), "the repeated hour must render"
    assert float(state) == pytest.approx(0.05), state
    assert api.call_count == calls_after_setup


async def test_repeated_reloads_do_not_accumulate_listeners_or_timers(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Reloading must not leave a second copy of the timers behind."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    tomorrow = date(2026, 9, 8)
    api.publish(today, day_slots(today, [0.20] * 24))
    # Both days published, so the schedule is just the two anchors and any
    # extra request would mean a duplicated timer.
    api.publish(tomorrow, day_slots(tomorrow, [0.30] * 24))

    entry = make_entry(update_time="06:00")
    freezer.move_to(at(2026, 9, 7, 8, 0))
    await setup_integration(hass, entry)

    for _ in range(3):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        # One update listener, however often the entry is reloaded.
        assert len(entry.update_listeners) == 1, len(entry.update_listeners)

    # Exactly one coordinator remains, and only its timers.
    assert len(hass.data[DOMAIN]) == 1
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator._render_unsub is not None

    # One afternoon anchor, not four - one per reload would mean a leak.
    calls_before = api.call_count
    await advance(hass, freezer, timedelta(hours=10), step=timedelta(minutes=10))
    assert api.call_count - calls_before == 1, api.call_count - calls_before

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert coordinator._tearing_down is True
    assert coordinator._render_unsub is None

    calls_after_unload = api.call_count
    await advance(hass, freezer, timedelta(hours=26), step=timedelta(minutes=30))
    assert api.call_count == calls_after_unload


async def test_binary_sensor_is_unknown_without_data(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A gap in the data must read as unknown, never as a confident off."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    # Coverage stops at 12:00, so from 12:00 there is no current slot.
    api.publish(today, day_slots(today, [0.20] * 12))

    freezer.move_to(at(2026, 9, 7, 13, 0))
    await setup_integration(hass, make_entry(update_time="06:00"))

    state = hass.states.get("sensor.test_electricity_cheapest_25_hours_today")
    assert state is not None
    assert state.state == "unknown", (
        "outside the covered range the percentile sensors must not report off"
    )


async def test_current_price_goes_stale_at_the_end_of_coverage(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """When the data runs out, the price must become unknown, not stick."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 12))

    freezer.move_to(at(2026, 9, 7, 11, 30))
    await setup_integration(hass, make_entry(update_time="06:00"))
    assert float(hass.states.get(CURRENT_PRICE).state) == pytest.approx(0.20)

    await advance(hass, freezer, timedelta(minutes=45), step=timedelta(minutes=5))

    assert hass.states.get(CURRENT_PRICE).state == "unknown", (
        "past the end of the covered range the price must not stay stale"
    )
