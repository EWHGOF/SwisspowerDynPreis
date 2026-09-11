"""Tests for the attributes a dashboard draws the future curve from.

The raw ``prices`` attribute has always carried tomorrow, but in the API's own
shape: the value nested under the tariff type and a component list, unfiltered,
with no split by day. Every card had to reimplement extract_slot_value in
JavaScript to use it. These tests pin down the flat attributes that replace
that work, and the two things a card cannot recover on its own - which
component a value belongs to, and whether tomorrow is published at all.
"""

from __future__ import annotations

from datetime import date, timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.swisspower_dynpreis.const import WINDOW_DAYS_FORWARD
from custom_components.swisspower_dynpreis.sensor import (
    SwisspowerDynPreisCurrentPriceSensor,
)

from .helpers import (
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

TODAY = date(2026, 9, 7)
TOMORROW = date(2026, 9, 8)
DAY_AFTER = date(2026, 9, 9)
THIRD_DAY = date(2026, 9, 10)

# The per-component current price sensor, which shares its curve with the plain
# one today.
ENERGY_PRICE = "sensor.test_electricity_energy_current_price"


def attrs(hass: HomeAssistant, entity_id: str = CURRENT_PRICE) -> dict:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return dict(state.attributes)


async def test_flat_attributes_split_the_curve_by_local_day(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """prices_today and prices_tomorrow are the two days, in the flat form."""
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))
    api.publish(TOMORROW, day_slots(TOMORROW, [0.30] * 24))

    freezer.move_to(at(2026, 9, 7, 15))
    await setup_integration(hass, make_entry())

    data = attrs(hass)

    assert len(data["prices_today"]) == 24
    assert len(data["prices_tomorrow"]) == 24

    first = data["prices_today"][0]
    assert set(first) == {"start", "end", "value"}
    assert first["start"] == "2026-09-07T00:00:00+02:00"
    # The end stays inclusive, the way every timestamp here is.
    assert first["end"] == "2026-09-07T00:59:59+02:00"
    assert first["value"] == 0.10

    # No day leaks into the other.
    assert all(item["start"].startswith("2026-09-07") for item in data["prices_today"])
    assert all(
        item["start"].startswith("2026-09-08") for item in data["prices_tomorrow"]
    )
    assert all(item["value"] == 0.30 for item in data["prices_tomorrow"])


async def test_upcoming_starts_at_the_running_slot(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """prices_upcoming drops what is over and keeps the price still in force.

    The slot covering now belongs in it: that is the price a dashboard is
    charging at, not a past one.
    """
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))
    api.publish(TOMORROW, day_slots(TOMORROW, [0.30] * 24))

    freezer.move_to(at(2026, 9, 7, 15, 30))
    await setup_integration(hass, make_entry())

    upcoming = attrs(hass)["prices_upcoming"]

    # 9 slots left today (15:00 through 23:00) plus all 24 of tomorrow.
    assert len(upcoming) == 9 + 24
    assert upcoming[0]["start"] == "2026-09-07T15:00:00+02:00"
    assert upcoming[0]["value"] == 0.25, "the hourly helper encodes the hour"

    now = dt_util.now()
    assert all(
        dt_util.parse_datetime(item["end"]) >= now for item in upcoming
    ), "a slot that has already ended is not upcoming"


async def test_the_curve_is_filtered_per_component(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Each component sensor shows its own values, not the whole payload.

    This is what the raw ``prices`` attribute cannot do: it is the unfiltered
    API payload, identical on every component sensor of a tariff type, so a
    chart built on it would draw two components on top of each other.
    """
    await set_time_zone(hass)
    energy = day_slots(TODAY, [0.10] * 24, component="energy")
    surcharge = day_slots(TODAY, [0.02] * 24, component="surcharge")
    # One slot per hour carrying both components, the way the API returns them.
    merged = []
    for base, extra in zip(energy, surcharge):
        slot = dict(base)
        slot["electricity"] = base["electricity"] + extra["electricity"]
        merged.append(slot)
    api.publish(TODAY, merged)

    freezer.move_to(at(2026, 9, 7, 10))
    await setup_integration(hass, make_entry())

    surcharge_price = "sensor.test_electricity_surcharge_current_price"
    energy_values = {item["value"] for item in attrs(hass, ENERGY_PRICE)["prices_today"]}
    surcharge_values = {
        item["value"] for item in attrs(hass, surcharge_price)["prices_today"]
    }

    assert energy_values == {0.10}
    assert surcharge_values == {0.02}

    # And the raw attribute is still the unfiltered payload on both, unchanged
    # for the cards that already read it.
    assert attrs(hass, ENERGY_PRICE)["prices"] == attrs(hass, surcharge_price)["prices"]


async def test_tomorrow_unpublished_is_empty_and_flagged(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """An empty list alone cannot say "not published yet" - tomorrow_valid can.

    Without the flag a dashboard cannot tell a supplier who has not published
    from one whose prices are genuinely zero.
    """
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))

    freezer.move_to(at(2026, 9, 7, 10))
    await setup_integration(hass, make_entry())

    data = attrs(hass)
    assert data["prices_tomorrow"] == []
    assert data["tomorrow_valid"] is False
    assert len(data["prices_today"]) == 24, "today must be unaffected"

    # Publishing tomorrow flips the flag on the next fetch.
    api.publish(TOMORROW, day_slots(TOMORROW, [0.30] * 24))
    freezer.move_to(at(2026, 9, 7, 14))
    await hass.async_block_till_done()
    coordinator = hass.data["swisspower_dynpreis"]
    entry_id = next(iter(coordinator))
    await coordinator[entry_id].async_refresh()
    await hass.async_block_till_done()

    data = attrs(hass)
    assert data["tomorrow_valid"] is True
    assert len(data["prices_tomorrow"]) == 24


async def test_tomorrow_valid_is_about_this_entity_only(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """One tariff type missing tomorrow must not flag the others as unpublished.

    The coordinator's tomorrow_complete asks about every configured type at
    once, because that is what decides whether to fetch again. Used as a
    per-entity attribute it would say "not published" on an electricity sensor
    that has a full curve for tomorrow, only because the grid tariff has not
    been published - a dashboard would then hide a chart it could draw.
    """
    await set_time_zone(hass)
    for tariff_type in ("electricity", "grid"):
        api.publish(TODAY, hourly(TODAY), tariff_type=tariff_type)
    # Only electricity publishes tomorrow.
    api.publish(TOMORROW, day_slots(TOMORROW, [0.30] * 24), tariff_type="electricity")

    freezer.move_to(at(2026, 9, 7, 15))
    await setup_integration(hass, make_entry(tariff_types=["electricity", "grid"]))

    electricity = attrs(hass)
    grid = attrs(hass, "sensor.test_grid_current_price")

    assert electricity["tomorrow_valid"] is True
    assert len(electricity["prices_tomorrow"]) == 24
    assert grid["tomorrow_valid"] is False
    assert grid["prices_tomorrow"] == []

    # The coordinator's own view stays the all-types one - the fetch schedule
    # depends on it, and it must still see a reason to ask again.
    coordinator = next(iter(hass.data["swisspower_dynpreis"].values()))
    assert coordinator.tomorrow_complete(coordinator.data) is False


async def test_a_partly_published_tomorrow_is_not_valid(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Half of tomorrow is not tomorrow, and coverage says how much arrived."""
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))
    # Twelve of tomorrow's 24 hours.
    api.publish(TOMORROW, day_slots(TOMORROW, [0.30] * 24)[:12])

    freezer.move_to(at(2026, 9, 7, 15))
    await setup_integration(hass, make_entry())

    data = attrs(hass)
    assert len(data["prices_tomorrow"]) == 12
    assert data["tomorrow_valid"] is False
    assert data["price_days"][1]["coverage"] == 0.5


async def test_price_days_summarizes_every_day_in_the_window(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """One entry per day the fetch window reaches, coverage included.

    coverage is what tells a half-published day from a complete one, so the
    window can reach further than the supplier publishes without needing a flag
    per day.
    """
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))
    api.publish(TOMORROW, day_slots(TOMORROW, [0.20, 0.40] * 12))

    freezer.move_to(at(2026, 9, 7, 15))
    await setup_integration(hass, make_entry())

    days = attrs(hass)["price_days"]
    assert len(days) == WINDOW_DAYS_FORWARD

    assert days[0]["date"] == "2026-09-07"
    assert days[0]["slots"] == 24
    assert days[0]["min"] == 0.10
    assert days[0]["max"] == 0.33
    assert days[0]["coverage"] == 1.0

    assert days[1]["date"] == "2026-09-08"
    # Rounded, so an alternating curve does not arrive as 0.30000000000000004.
    assert days[1]["average"] == 0.30
    assert days[1]["coverage"] == 1.0

    # The two days beyond tomorrow are inside the window but unpublished:
    # reported as empty rather than missing, so a card can say so.
    for index, day in ((2, "2026-09-09"), (3, "2026-09-10")):
        assert days[index]["date"] == day
        assert days[index]["slots"] == 0
        assert days[index]["average"] is None
        assert days[index]["min"] is None
        assert days[index]["coverage"] == 0.0


async def test_a_supplier_publishing_three_days_ahead_shows_up(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """The window reaches three days ahead, for a longer optimization horizon.

    Nothing beyond the window constant had to change for this: prices_upcoming
    and price_days are both derived from it. A day-ahead supplier still leaves
    the far days empty, which costs nothing - the window is a parameter of the
    same request, and only tomorrow is ever chased for.
    """
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))
    api.publish(TOMORROW, day_slots(TOMORROW, [0.30] * 24))
    api.publish(DAY_AFTER, day_slots(DAY_AFTER, [0.50] * 24))
    api.publish(THIRD_DAY, day_slots(THIRD_DAY, [0.70] * 24))

    freezer.move_to(at(2026, 9, 7, 23, 30))
    await setup_integration(hass, make_entry())

    data = attrs(hass)
    # The last slot of today, plus all three full days ahead.
    assert len(data["prices_upcoming"]) == 1 + 24 + 24 + 24
    assert data["price_days"][2]["average"] == 0.50
    assert data["price_days"][2]["coverage"] == 1.0
    assert data["price_days"][3]["date"] == "2026-09-10"
    assert data["price_days"][3]["average"] == 0.70
    assert data["price_days"][3]["coverage"] == 1.0
    # And neither is mistaken for tomorrow.
    assert all(item["value"] == 0.30 for item in data["prices_tomorrow"])


async def test_quarter_hourly_prices_keep_their_resolution(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """96 slots stay 96 slots, and coverage still reads as a full day."""
    await set_time_zone(hass)
    api.publish(TODAY, day_slots(TODAY, [0.10] * 96, slot_minutes=15))

    freezer.move_to(at(2026, 9, 7, 10))
    await setup_integration(hass, make_entry())

    data = attrs(hass)
    assert len(data["prices_today"]) == 96
    assert data["price_days"][0]["slots"] == 96
    assert data["price_days"][0]["coverage"] == 1.0
    assert data["prices_today"][1]["start"] == "2026-09-07T00:15:00+02:00"


def test_the_curves_are_kept_out_of_the_recorder() -> None:
    """Four more curves must not land in the long-term database.

    They are rewritten at every price boundary - up to 96 times a day - which
    is the same reason the raw ``prices`` attribute is excluded. tomorrow_valid
    is not excluded: one bool that flips once a day is worth having.

    Asserted on the declaration rather than on the entity Home Assistant built
    from it: the combined set lives in a name-mangled private attribute, and
    reaching into that would tie this test to one HA version.
    """
    declared = SwisspowerDynPreisCurrentPriceSensor._unrecorded_attributes
    assert declared >= {
        "prices",
        "prices_today",
        "prices_tomorrow",
        "prices_upcoming",
        "price_days",
    }
    assert "tomorrow_valid" not in declared


async def test_a_midnight_crossing_moves_tomorrow_into_today(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """The split follows the local day, not the day the data was fetched."""
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))
    api.publish(TOMORROW, day_slots(TOMORROW, [0.30] * 24))

    freezer.move_to(at(2026, 9, 7, 23, 30))
    await setup_integration(hass, make_entry())
    assert all(item["value"] == 0.30 for item in attrs(hass)["prices_tomorrow"])

    # Past midnight, without a new fetch: the render timer fires on the
    # boundary and the same cached curve must be split the other way. advance()
    # rather than a bare tick, so Home Assistant's time listeners actually run.
    calls_before = api.call_count
    await advance(hass, freezer, timedelta(minutes=45))
    assert api.call_count == calls_before, "re-rendering must not refetch"

    data = attrs(hass)
    assert all(item["value"] == 0.30 for item in data["prices_today"])
    assert data["price_days"][0]["date"] == "2026-09-08"
