"""Tests for adding VAT to the net prices the API publishes.

Two ways in: a rate typed into the options, or an entity to read it from. The
questions worth pinning down are what the rate is applied to (every derived
price, never the raw API payload), what it is deliberately not applied to
(feed-in, and the on/off sensors that only compare prices against each other),
and what happens to the price when the entity holding the rate stops answering.
"""

from __future__ import annotations

from datetime import date

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.swisspower_dynpreis.const import (
    CONF_UPDATE_TIME,
    CONF_UPDATE_TIME_PM,
    CONF_QUERY_YEAR,
    CONF_VAT_ENTITY,
    CONF_VAT_RATE,
    CONF_VAT_TARIFF_TYPES,
    DOMAIN,
)
from custom_components.swisspower_dynpreis.pricing import (
    extract_slot_value,
    normalize_price_slots,
)
from custom_components.swisspower_dynpreis.vat import (
    apply_multiplier,
    parse_vat_rate,
    vat_multiplier,
)

from .helpers import (
    AVG_TODAY,
    CURRENT_PRICE,
    FakeApi,
    all_entities_enabled,
    at,
    day_slots,
    hourly,
    make_entry,
    set_time_zone,
    setup_integration,
)

TODAY = date(2026, 9, 7)
RATE_ENTITY = "input_number.mwst"

# The Swiss rate since 2024, and the one every expectation below is built on.
RATE = 8.1
FACTOR = 1.081

FEED_IN_PRICE = "sensor.test_feed_in_current_price"
# The per-component current price sensor of the electricity type.
ENERGY_PRICE = "sensor.test_electricity_energy_current_price"
CHEAPEST_25 = "binary_sensor.test_electricity_cheapest_25_hours_today"


def attrs(hass: HomeAssistant, entity_id: str = CURRENT_PRICE) -> dict:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return dict(state.attributes)


def value(hass: HomeAssistant, entity_id: str) -> float:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return float(state.state)


# ----------------------------------------------------------------------
# The maths, without a Home Assistant instance
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (8.1, 8.1),
        ("8.1", 8.1),
        # A sensor with a unit renders its state without one, but a template
        # sensor may well not.
        ("8.1 %", 8.1),
        (0, 0.0),
        ("0", 0.0),
        (100, 100.0),
    ],
)
def test_parse_vat_rate_accepts_a_rate(raw, expected) -> None:
    assert parse_vat_rate(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        # The two states Home Assistant writes for an entity that cannot
        # answer. Read as a number they would mean "no VAT", which is a price
        # change rather than a gap.
        "unknown",
        "unavailable",
        "acht Komma eins",
        -1,
        # A sensor reporting 810 for 8.1% would otherwise multiply every price
        # by nine.
        101,
        float("nan"),
        float("inf"),
        True,
    ],
)
def test_parse_vat_rate_rejects_what_cannot_be_a_rate(raw) -> None:
    assert parse_vat_rate(raw) is None


def test_a_missing_rate_is_not_a_rate_of_zero() -> None:
    """The distinction the whole fallback chain rests on."""
    assert parse_vat_rate("unknown") is None
    assert parse_vat_rate("0") == 0.0


def test_vat_multiplier() -> None:
    assert vat_multiplier(None) == 1.0
    assert vat_multiplier(0.0) == 1.0
    assert vat_multiplier(RATE) == pytest.approx(FACTOR)


def test_apply_multiplier_leaves_a_missing_price_missing() -> None:
    assert apply_multiplier(None, FACTOR) is None


def test_apply_multiplier_is_a_no_op_at_one() -> None:
    """Not even rounded: an install without VAT keeps the API's own number."""
    assert apply_multiplier(0.123456789, 1.0) == 0.123456789


def test_apply_multiplier_rounds_away_the_float_noise() -> None:
    """0.1475 * 1.081 is 0.15944750000000002 in binary floating point.

    Which decimal a value exactly on the boundary lands on is the rounding
    mode's business; what matters here is that no attribute and no chart
    tooltip carries the seventeen-digit tail.
    """
    grossed = apply_multiplier(0.1475, FACTOR)
    assert grossed == round(grossed, 6)
    assert grossed == pytest.approx(0.1594475, abs=5e-7)


def test_extract_slot_value_applies_the_multiplier() -> None:
    slot = {
        "start_timestamp": "2026-09-07T00:00:00+02:00",
        "end_timestamp": "2026-09-07T00:59:59+02:00",
        "electricity": [{"component": "energy", "unit": "CHF/kWh", "value": 0.10}],
    }
    assert extract_slot_value(slot, "electricity", None) == 0.10
    assert extract_slot_value(slot, "electricity", None, FACTOR) == 0.1081


def test_normalize_price_slots_applies_the_multiplier() -> None:
    slots = day_slots(TODAY, [0.10, 0.20])
    assert [slot.value for slot in normalize_price_slots(slots, "electricity")] == [
        0.10,
        0.20,
    ]
    normalized = normalize_price_slots(slots, "electricity", None, FACTOR)
    assert [slot.value for slot in normalized] == [0.1081, 0.2162]


# ----------------------------------------------------------------------
# A fixed rate from the options
# ----------------------------------------------------------------------


async def test_a_fixed_rate_reaches_every_derived_price(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """State, statistics and the chart attributes all carry the same VAT."""
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))

    freezer.move_to(at(2026, 9, 7, 10, 30))
    await setup_integration(hass, make_entry(options={CONF_VAT_RATE: RATE}))

    # 10:00 of hourly(): 0.10 + 10 * 0.01.
    assert value(hass, CURRENT_PRICE) == pytest.approx(0.20 * FACTOR)

    data = attrs(hass)
    assert data["current_value"] == pytest.approx(0.20 * FACTOR)
    assert data["prices_today"][0]["value"] == pytest.approx(0.10 * FACTOR)
    assert data["prices_upcoming"][0]["value"] == pytest.approx(0.20 * FACTOR)
    assert data["price_days"][0]["average"] == pytest.approx(0.215 * FACTOR)
    assert data["price_days"][0]["min"] == pytest.approx(0.10 * FACTOR)
    assert data["price_days"][0]["max"] == pytest.approx(0.33 * FACTOR)
    assert data["vat_rate"] == RATE

    # The average of 0.10 .. 0.33 is 0.215.
    assert value(hass, AVG_TODAY) == pytest.approx(0.215 * FACTOR)
    assert attrs(hass, AVG_TODAY)["max_price"] == pytest.approx(0.33 * FACTOR)


async def test_the_component_sensors_carry_it_too(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """One tariff type can have several component sensors; none may be net."""
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))

    freezer.move_to(at(2026, 9, 7, 10, 30))
    await setup_integration(hass, make_entry(options={CONF_VAT_RATE: RATE}))

    assert value(hass, ENERGY_PRICE) == pytest.approx(0.20 * FACTOR)
    assert attrs(hass, ENERGY_PRICE)["prices_today"][0]["value"] == pytest.approx(
        0.10 * FACTOR
    )


async def test_the_raw_prices_attribute_stays_net(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """``prices`` is what the supplier sent, and has to stay comparable to it.

    It is the attribute the diagnostic raw response sensor is checked against;
    quietly grossing it up would make the two disagree.
    """
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))

    freezer.move_to(at(2026, 9, 7, 10, 30))
    await setup_integration(hass, make_entry(options={CONF_VAT_RATE: RATE}))

    raw = attrs(hass)["prices"][0]
    assert raw["electricity"][0]["value"] == 0.10


async def test_no_rate_configured_leaves_the_prices_untouched(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """The default: every number is the API's own, to the last bit."""
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))

    freezer.move_to(at(2026, 9, 7, 10, 30))
    await setup_integration(hass, make_entry())

    assert value(hass, CURRENT_PRICE) == 0.20
    assert attrs(hass)["vat_rate"] == 0.0
    assert value(hass, AVG_TODAY) == pytest.approx(0.215)


async def test_feed_in_is_left_out_by_default(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A feed-in tariff is a credit, not a purchase - no VAT unless asked."""
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))
    api.publish(
        TODAY,
        day_slots(TODAY, [0.08] * 24, tariff_type="feed_in"),
        tariff_type="feed_in",
    )

    freezer.move_to(at(2026, 9, 7, 10, 30))
    await setup_integration(
        hass,
        make_entry(
            tariff_types=["electricity", "feed_in"],
            options={CONF_VAT_RATE: RATE},
        ),
    )

    assert value(hass, CURRENT_PRICE) == pytest.approx(0.20 * FACTOR)
    assert value(hass, FEED_IN_PRICE) == 0.08
    assert attrs(hass, FEED_IN_PRICE)["vat_rate"] == 0.0


async def test_feed_in_can_be_opted_in(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A VAT-registered household ticks it, and then it is applied there too."""
    await set_time_zone(hass)
    api.publish(
        TODAY,
        day_slots(TODAY, [0.08] * 24, tariff_type="feed_in"),
        tariff_type="feed_in",
    )

    freezer.move_to(at(2026, 9, 7, 10, 30))
    await setup_integration(
        hass,
        make_entry(
            tariff_types=["feed_in"],
            options={CONF_VAT_RATE: RATE, CONF_VAT_TARIFF_TYPES: ["feed_in"]},
        ),
    )

    assert value(hass, FEED_IN_PRICE) == pytest.approx(0.08 * FACTOR)


async def test_an_empty_type_list_is_a_choice_not_a_missing_option(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Unticking every type turns the rate off without clearing it."""
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))

    freezer.move_to(at(2026, 9, 7, 10, 30))
    await setup_integration(
        hass,
        make_entry(options={CONF_VAT_RATE: RATE, CONF_VAT_TARIFF_TYPES: []}),
    )

    assert value(hass, CURRENT_PRICE) == 0.20


async def test_the_on_off_sensors_are_unaffected(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """They compare today's prices against each other, so a factor cancels."""
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))

    freezer.move_to(at(2026, 9, 7, 1, 30))
    with all_entities_enabled():
        await setup_integration(hass, make_entry(options={CONF_VAT_RATE: RATE}))

    # 01:00 is the second cheapest of 24 hourly prices, so inside the cheapest
    # quarter of the day with or without VAT.
    state = hass.states.get(CHEAPEST_25)
    assert state is not None
    assert state.state == "on"


# ----------------------------------------------------------------------
# A rate read from an entity
# ----------------------------------------------------------------------


async def test_the_rate_is_read_from_the_configured_entity(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))
    hass.states.async_set(RATE_ENTITY, "8.1")

    freezer.move_to(at(2026, 9, 7, 10, 30))
    await setup_integration(hass, make_entry(options={CONF_VAT_ENTITY: RATE_ENTITY}))

    assert value(hass, CURRENT_PRICE) == pytest.approx(0.20 * FACTOR)
    assert attrs(hass)["vat_rate"] == RATE


async def test_the_entity_wins_over_the_fixed_rate(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))
    hass.states.async_set(RATE_ENTITY, "2.6")

    freezer.move_to(at(2026, 9, 7, 10, 30))
    await setup_integration(
        hass,
        make_entry(options={CONF_VAT_RATE: RATE, CONF_VAT_ENTITY: RATE_ENTITY}),
    )

    assert value(hass, CURRENT_PRICE) == pytest.approx(0.20 * 1.026)


async def test_a_new_rate_re_renders_without_asking_the_api(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """The price is a function of (slots, now, rate) - a rate change is a render."""
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))
    hass.states.async_set(RATE_ENTITY, "7.7")

    freezer.move_to(at(2026, 9, 7, 10, 30))
    await setup_integration(hass, make_entry(options={CONF_VAT_ENTITY: RATE_ENTITY}))

    assert value(hass, CURRENT_PRICE) == pytest.approx(0.20 * 1.077)
    calls = api.call_count

    hass.states.async_set(RATE_ENTITY, "8.1")
    await hass.async_block_till_done()

    assert value(hass, CURRENT_PRICE) == pytest.approx(0.20 * FACTOR)
    assert value(hass, AVG_TODAY) == pytest.approx(0.215 * FACTOR)
    assert api.call_count == calls


async def test_an_unavailable_entity_keeps_the_last_rate(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A gap in the entity must not read as "VAT is zero now".

    That would take 8.1% off every price for the length of a restart and then
    put it back, which is indistinguishable from the supplier having changed
    the tariff.
    """
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))
    hass.states.async_set(RATE_ENTITY, "8.1")

    freezer.move_to(at(2026, 9, 7, 10, 30))
    await setup_integration(hass, make_entry(options={CONF_VAT_ENTITY: RATE_ENTITY}))

    hass.states.async_set(RATE_ENTITY, "unavailable")
    await hass.async_block_till_done()

    assert value(hass, CURRENT_PRICE) == pytest.approx(0.20 * FACTOR)

    hass.states.async_remove(RATE_ENTITY)
    await hass.async_block_till_done()

    assert value(hass, CURRENT_PRICE) == pytest.approx(0.20 * FACTOR)


async def test_an_entity_that_never_answers_falls_back_to_the_fixed_rate(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Which is the whole of a restart, before the input_number is restored."""
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))

    freezer.move_to(at(2026, 9, 7, 10, 30))
    await setup_integration(
        hass,
        make_entry(options={CONF_VAT_RATE: 2.6, CONF_VAT_ENTITY: RATE_ENTITY}),
    )

    assert value(hass, CURRENT_PRICE) == pytest.approx(0.20 * 1.026)

    hass.states.async_set(RATE_ENTITY, "8.1")
    await hass.async_block_till_done()

    assert value(hass, CURRENT_PRICE) == pytest.approx(0.20 * FACTOR)


async def test_an_out_of_range_entity_state_is_ignored(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """810 for 8.1% would otherwise multiply every price by nine."""
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))
    hass.states.async_set(RATE_ENTITY, "8.1")

    freezer.move_to(at(2026, 9, 7, 10, 30))
    await setup_integration(hass, make_entry(options={CONF_VAT_ENTITY: RATE_ENTITY}))

    hass.states.async_set(RATE_ENTITY, "810")
    await hass.async_block_till_done()

    assert value(hass, CURRENT_PRICE) == pytest.approx(0.20 * FACTOR)


async def test_diagnostics_name_the_rate_and_where_it_came_from(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))
    hass.states.async_set(RATE_ENTITY, "8.1")

    freezer.move_to(at(2026, 9, 7, 10, 30))
    entry = make_entry(options={CONF_VAT_ENTITY: RATE_ENTITY})
    await setup_integration(hass, entry)

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.vat_state() == {
        "rate": RATE,
        "source": "entity",
        "entity_id": RATE_ENTITY,
        "tariff_types": ["electricity"],
    }


# ----------------------------------------------------------------------
# The options form
# ----------------------------------------------------------------------


async def test_the_options_form_offers_the_vat_fields(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))

    freezer.move_to(at(2026, 9, 7, 10, 30))
    entry = make_entry()
    await setup_integration(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM

    keys = {str(key) for key in result["data_schema"].schema}
    assert {CONF_VAT_RATE, CONF_VAT_ENTITY, CONF_VAT_TARIFF_TYPES} <= keys

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_UPDATE_TIME: "06:00:00",
            CONF_UPDATE_TIME_PM: "14:00:00",
            CONF_VAT_RATE: RATE,
            CONF_VAT_ENTITY: RATE_ENTITY,
            CONF_VAT_TARIFF_TYPES: ["electricity"],
            CONF_QUERY_YEAR: "",
        },
    )
    await hass.async_block_till_done()

    assert entry.options[CONF_VAT_RATE] == RATE
    assert entry.options[CONF_VAT_ENTITY] == RATE_ENTITY
    # Saved options reload the entry, so the new rate is live right away.
    assert value(hass, CURRENT_PRICE) == pytest.approx(0.20 * FACTOR)


async def test_the_vat_entity_can_be_cleared_again(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A ``default`` on that field would make the entity impossible to remove."""
    await set_time_zone(hass)
    api.publish(TODAY, hourly(TODAY))
    hass.states.async_set(RATE_ENTITY, "8.1")

    freezer.move_to(at(2026, 9, 7, 10, 30))
    entry = make_entry(options={CONF_VAT_ENTITY: RATE_ENTITY})
    await setup_integration(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_UPDATE_TIME: "06:00:00",
            CONF_UPDATE_TIME_PM: "14:00:00",
            CONF_VAT_RATE: 0,
            CONF_VAT_TARIFF_TYPES: ["electricity"],
            CONF_QUERY_YEAR: "",
        },
    )
    await hass.async_block_till_done()

    assert CONF_VAT_ENTITY not in entry.options
    assert value(hass, CURRENT_PRICE) == 0.20
