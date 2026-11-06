"""Tests for the power tariff (Leistungstarif) the API delivers in the grid list.

Where the grid operator charges a power tariff, the API adds a second element to
the ``grid`` list of every slot: component ``power``, unit ``CHF_kW_m`` (CHF per
kW and month), next to the ``work`` price in ``CHF_kWh``. It is not summed into
``dso`` or ``integrated``. Two things follow, and both are pinned here: the
power tariff is offered as entities of its own, in its own unit; and it never
leaks into anything that is a price per kWh.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.swisspower_dynpreis.const import (
    CONF_VAT_RATE,
    DOMAIN,
    UNIT_ENERGY_PRICE,
    UNIT_POWER_PRICE,
)
from custom_components.swisspower_dynpreis.pricing import (
    extract_slot_value,
    is_power_price,
    normalize_price_slots,
    normalize_unit,
)

from .helpers import (
    FakeApi,
    all_entities_enabled,
    at,
    local_tz,
    make_entry,
    set_time_zone,
    setup_integration,
)

TODAY = date(2026, 9, 7)
TOMORROW = date(2026, 9, 8)

GRID_PRICE = "sensor.test_grid_current_price"
GRID_WORK_PRICE = "sensor.test_grid_work_current_price"
POWER_TARIFF = "sensor.test_grid_power_tariff"
POWER_AVG_TODAY = "sensor.test_grid_power_tariff_average_today"
POWER_AVG_TOMORROW = "sensor.test_grid_power_tariff_average_tomorrow"
GRID_AVG_TODAY = "sensor.test_grid_average_price_today"
GRID_LOWEST_2H = "sensor.test_grid_lowest_2h_window_today"


def grid_slots(
    day: date,
    work: list[float],
    power: list[float] | None,
    *,
    power_first: bool = True,
    work_unit: str = "CHF_kWh",
    power_unit: str = "CHF_kW_m",
) -> list[dict[str, Any]]:
    """Hourly grid slots in the shape the API documents.

    ``power_first`` defaults to True on purpose: with the power element ahead
    of the work price, "the first element with a value" - what the energy
    price used to be read as - is the power tariff.
    """
    midnight = datetime(day.year, day.month, day.day, tzinfo=local_tz())
    step = timedelta(hours=1)
    slots: list[dict[str, Any]] = []
    for index, work_value in enumerate(work):
        start = midnight + index * step
        elements = [{"value": work_value, "unit": work_unit, "component": "work"}]
        if power is not None:
            power_element = {
                "value": power[index],
                "unit": power_unit,
                "component": "power",
            }
            if power_first:
                elements.insert(0, power_element)
            else:
                elements.append(power_element)
        slots.append(
            {
                "start_timestamp": start.isoformat(),
                "end_timestamp": (start + step - timedelta(seconds=1)).isoformat(),
                "grid": elements,
            }
        )
    return slots


def work_curve() -> list[float]:
    """0.05 .. 0.28 CHF/kWh, encoding the hour."""
    return [round(0.05 + hour * 0.01, 4) for hour in range(24)]


def power_curve() -> list[float]:
    """A power tariff that is high in the evening: 2.0, and 9.0 from 17 to 20."""
    return [9.0 if 17 <= hour <= 20 else 2.0 for hour in range(24)]


def state_value(hass: HomeAssistant, entity_id: str) -> float:
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} does not exist"
    return float(state.state)


# ----------------------------------------------------------------------
# Pricing helpers, no Home Assistant instance
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CHF_kWh", UNIT_ENERGY_PRICE),
        ("CHF/kWh", UNIT_ENERGY_PRICE),
        ("chf_kwh", UNIT_ENERGY_PRICE),
        ("CHF_kW_m", UNIT_POWER_PRICE),
        ("CHF/kW/month", UNIT_POWER_PRICE),
        ("EUR/kWh", "EUR/kWh"),
        (None, None),
        (12, None),
    ],
)
def test_normalize_unit(raw: Any, expected: str | None) -> None:
    assert normalize_unit(raw) == expected


def test_a_power_element_is_recognized_by_component_or_unit() -> None:
    assert is_power_price({"component": "power", "value": 1.0})
    assert is_power_price({"component": "peak", "unit": "CHF_kW_m", "value": 1.0})
    assert not is_power_price({"component": "work", "unit": "CHF_kWh", "value": 1.0})
    assert not is_power_price({"component": "energy", "value": 1.0})


@pytest.mark.parametrize("power_first", [True, False])
def test_the_energy_price_never_reads_the_power_element(power_first: bool) -> None:
    """Whichever position the API puts it in, the plain price is the work price."""
    slot = grid_slots(TODAY, [0.11], [7.5], power_first=power_first)[0]
    assert extract_slot_value(slot, "grid", None) == 0.11
    assert extract_slot_value(slot, "grid", "work") == 0.11
    assert extract_slot_value(slot, "grid", "power") == 7.5


def test_a_power_element_without_a_unit_is_still_not_an_energy_price() -> None:
    slot = {
        "start_timestamp": "2026-09-07T00:00:00+02:00",
        "end_timestamp": "2026-09-07T00:59:59+02:00",
        "grid": [
            {"value": 7.5, "component": "power"},
            {"value": 0.11, "component": "work"},
        ],
    }
    assert extract_slot_value(slot, "grid", None) == 0.11


def test_a_slot_with_only_a_power_tariff_has_no_energy_price() -> None:
    """Better unknown than a CHF/kW/month figure shown as CHF/kWh."""
    slot = {
        "start_timestamp": "2026-09-07T00:00:00+02:00",
        "end_timestamp": "2026-09-07T00:59:59+02:00",
        "grid": [{"value": 7.5, "unit": "CHF_kW_m", "component": "power"}],
    }
    assert extract_slot_value(slot, "grid", None) is None
    assert extract_slot_value(slot, "grid", "power") == 7.5


def test_the_new_unit_spelling_is_an_energy_price() -> None:
    """"CHF_kWh" is how the API spells it now; the old check knew only "CHF/kWh"."""
    slot = {
        "start_timestamp": "2026-09-07T00:00:00+02:00",
        "end_timestamp": "2026-09-07T00:59:59+02:00",
        "grid": [
            {"value": 0.30, "unit": "CHF_other", "component": "levy"},
            {"value": 0.11, "unit": "CHF_kWh", "component": "work"},
        ],
    }
    assert extract_slot_value(slot, "grid", None) == 0.11


def test_the_power_curve_and_the_work_curve_stay_apart() -> None:
    slots = grid_slots(TODAY, work_curve(), power_curve())
    work = normalize_price_slots(slots, "grid")
    power = normalize_price_slots(slots, "grid", "power")
    assert [slot.value for slot in work] == work_curve()
    assert [slot.value for slot in power] == power_curve()


def test_the_power_tariff_carries_vat_like_the_rest_of_grid() -> None:
    slot = grid_slots(TODAY, [0.10], [5.0])[0]
    assert extract_slot_value(slot, "grid", "power", 1.081) == pytest.approx(5.405)


# ----------------------------------------------------------------------
# Entities
# ----------------------------------------------------------------------


async def test_the_power_tariff_gets_entities_of_its_own(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    await set_time_zone(hass)
    api.publish(TODAY, grid_slots(TODAY, work_curve(), power_curve()), tariff_type="grid")
    api.publish(
        TOMORROW,
        grid_slots(TOMORROW, work_curve(), [3.0] * 24),
        tariff_type="grid",
    )

    freezer.move_to(at(2026, 9, 7, 18, 30))
    await setup_integration(hass, make_entry(tariff_types=["grid"]))

    state = hass.states.get(POWER_TARIFF)
    assert state is not None
    assert float(state.state) == 9.0
    assert state.attributes["unit_of_measurement"] == UNIT_POWER_PRICE
    assert state.attributes["unit"] == UNIT_POWER_PRICE
    assert state.attributes["component"] == "power"
    assert state.attributes["current_value"] == 9.0
    assert [slot["value"] for slot in state.attributes["prices_today"]] == power_curve()
    assert [slot["value"] for slot in state.attributes["prices_tomorrow"]] == [3.0] * 24
    assert state.attributes["tomorrow_valid"] is True
    assert state.attributes["price_days"][0]["max"] == 9.0
    assert state.attributes["price_days"][0]["min"] == 2.0

    # Daily averages: four hours at 9.0, twenty at 2.0.
    avg_today = hass.states.get(POWER_AVG_TODAY)
    assert avg_today is not None
    assert float(avg_today.state) == pytest.approx((4 * 9.0 + 20 * 2.0) / 24)
    assert avg_today.attributes["unit_of_measurement"] == UNIT_POWER_PRICE
    assert avg_today.attributes["max_price"] == 9.0
    assert state_value(hass, POWER_AVG_TOMORROW) == pytest.approx(3.0)


async def test_every_kwh_price_stays_the_work_price(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """The power element sits first in the list, which is the dangerous order."""
    await set_time_zone(hass)
    api.publish(TODAY, grid_slots(TODAY, work_curve(), power_curve()), tariff_type="grid")

    freezer.move_to(at(2026, 9, 7, 18, 30))
    await setup_integration(hass, make_entry(tariff_types=["grid"]))

    # 18:00 of work_curve(): 0.05 + 18 * 0.01.
    assert state_value(hass, GRID_PRICE) == pytest.approx(0.23)
    assert hass.states.get(GRID_PRICE).attributes["unit_of_measurement"] == (
        UNIT_ENERGY_PRICE
    )
    assert state_value(hass, GRID_WORK_PRICE) == pytest.approx(0.23)
    assert state_value(hass, GRID_AVG_TODAY) == pytest.approx(
        sum(work_curve()) / 24
    )
    # The cheapest two hours of the work curve are 00:00 and 01:00.
    assert state_value(hass, GRID_LOWEST_2H) == pytest.approx(0.055)
    prices_today = hass.states.get(GRID_PRICE).attributes["prices_today"]
    assert [slot["value"] for slot in prices_today] == work_curve()


async def test_the_on_off_sensors_ignore_the_power_tariff(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """The percentile sensors compare work prices, not the power tariff.

    At 11:00 the power tariff sits on its daily minimum, tied with nineteen
    other hours, so read off the power element "cheapest 10%" would be on.
    The work price at 11:00 is the twelfth cheapest of the day: outside the
    cheapest 10%, exactly on the edge of the cheapest 50%.
    """
    await set_time_zone(hass)
    api.publish(TODAY, grid_slots(TODAY, work_curve(), power_curve()), tariff_type="grid")

    freezer.move_to(at(2026, 9, 7, 11, 30))
    with all_entities_enabled():
        await setup_integration(hass, make_entry(tariff_types=["grid"]))

    assert hass.states.get("binary_sensor.test_grid_cheapest_10_hours_today").state == (
        "off"
    )
    assert hass.states.get("binary_sensor.test_grid_cheapest_50_hours_today").state == (
        "on"
    )


async def test_vat_reaches_the_power_tariff(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A power tariff is a purchase from the grid operator like the work price."""
    await set_time_zone(hass)
    api.publish(TODAY, grid_slots(TODAY, work_curve(), power_curve()), tariff_type="grid")

    freezer.move_to(at(2026, 9, 7, 18, 30))
    await setup_integration(
        hass, make_entry(tariff_types=["grid"], options={CONF_VAT_RATE: 8.1})
    )

    assert state_value(hass, POWER_TARIFF) == pytest.approx(9.0 * 1.081)
    assert hass.states.get(POWER_TARIFF).attributes["vat_rate"] == 8.1
    assert state_value(hass, GRID_PRICE) == pytest.approx(0.23 * 1.081)
    # The raw payload stays net, as for every other price.
    raw = hass.states.get(POWER_TARIFF).attributes["prices"][18]["grid"]
    assert {"value": 9.0, "unit": "CHF_kW_m", "component": "power"} in raw


async def test_no_power_tariff_no_power_entities(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A grid without a power tariff gets nothing that would stay unknown forever."""
    await set_time_zone(hass)
    api.publish(TODAY, grid_slots(TODAY, work_curve(), None), tariff_type="grid")

    freezer.move_to(at(2026, 9, 7, 18, 30))
    await setup_integration(hass, make_entry(tariff_types=["grid"]))

    assert hass.states.get(POWER_TARIFF) is None
    assert hass.states.get(POWER_AVG_TODAY) is None
    assert state_value(hass, GRID_PRICE) == pytest.approx(0.23)


async def test_a_power_tariff_that_appears_later_is_added_without_a_restart(
    hass: HomeAssistant,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The grid operator assigns a power tariff; the next fetch carries it."""
    await set_time_zone(hass)
    api.publish(TODAY, grid_slots(TODAY, work_curve(), None), tariff_type="grid")

    freezer.move_to(at(2026, 9, 7, 18, 30))
    await setup_integration(hass, make_entry(tariff_types=["grid"]))
    assert hass.states.get(POWER_TARIFF) is None

    api.publish(TODAY, grid_slots(TODAY, work_curve(), power_curve()), tariff_type="grid")
    await hass.services.async_call(
        Platform.BUTTON,
        "press",
        {"entity_id": "button.test_refresh_now"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert state_value(hass, POWER_TARIFF) == 9.0
    assert state_value(hass, POWER_AVG_TODAY) == pytest.approx(
        (4 * 9.0 + 20 * 2.0) / 24
    )

    # A second fetch with the same components adds nothing twice. Counting
    # states would not show it - Home Assistant drops an entity whose unique id
    # already exists - but it logs an error for every one it drops.
    caplog.clear()
    await hass.services.async_call(
        Platform.BUTTON,
        "press",
        {"entity_id": "button.test_refresh_now"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert "already exists" not in caplog.text


async def test_a_fresh_install_gets_the_new_entity_ids(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """The unique id of the current value is the one the component entity had."""
    await set_time_zone(hass)
    api.publish(TODAY, grid_slots(TODAY, work_curve(), power_curve()), tariff_type="grid")
    entry = make_entry(tariff_types=["grid"])

    freezer.move_to(at(2026, 9, 7, 18, 30))
    await setup_integration(hass, entry)

    registry = er.async_get(hass)
    assert registry.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{entry.entry_id}_grid_power_current_price"
    ) == POWER_TARIFF
    assert registry.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{entry.entry_id}_grid_power_avg_today"
    ) == POWER_AVG_TODAY


async def test_an_existing_power_component_entity_keeps_its_identity(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Before this change the power element already made a component entity.

    It was named "grid power Current price" and claimed CHF/kWh. An install
    that has it keeps that entity id and its history, gets the right unit, and
    does not get a second entity for the same value.
    """
    await set_time_zone(hass)
    api.publish(TODAY, grid_slots(TODAY, work_curve(), power_curve()), tariff_type="grid")
    entry = make_entry(tariff_types=["grid"])
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    legacy = registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        f"{entry.entry_id}_grid_power_current_price",
        config_entry=entry,
        suggested_object_id="test_grid_power_current_price",
        unit_of_measurement=UNIT_ENERGY_PRICE,
    )
    assert legacy.entity_id == "sensor.test_grid_power_current_price"

    freezer.move_to(at(2026, 9, 7, 18, 30))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(legacy.entity_id)
    assert state is not None
    assert float(state.state) == 9.0
    assert state.attributes["unit_of_measurement"] == UNIT_POWER_PRICE
    assert hass.states.get(POWER_TARIFF) is None
    # The averages are new, so they get the new names.
    assert hass.states.get(POWER_AVG_TODAY) is not None


async def test_the_power_tariff_is_on_by_default(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Its state is a number, so it follows the rule for numeric entities."""
    await set_time_zone(hass)
    api.publish(TODAY, grid_slots(TODAY, work_curve(), power_curve()), tariff_type="grid")

    freezer.move_to(at(2026, 9, 7, 18, 30))
    await setup_integration(hass, make_entry(tariff_types=["grid"]))

    registry = er.async_get(hass)
    for entity_id in (POWER_TARIFF, POWER_AVG_TODAY, POWER_AVG_TOMORROW):
        entry = registry.async_get(entity_id)
        assert entry is not None, entity_id
        assert entry.disabled_by is None, entity_id
