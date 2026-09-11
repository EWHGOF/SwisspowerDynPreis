"""Tests for the manual refresh button.

Everything else fetches on a clock. This is the one control that says "now", so
what matters is that a press really reaches the API - not a debouncer, not a
backoff that has given up - and that the user is told when it did not work.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from aiohttp import ClientError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.swisspower_dynpreis.const import BACKOFF_MAX_TRIES, DOMAIN

from .helpers import (
    AVG_TOMORROW,
    CURRENT_PRICE,
    FakeApi,
    advance,
    at,
    day_slots,
    make_entry,
    set_time_zone,
    setup_integration,
)

REFRESH = "button.test_refresh_now"

TODAY = date(2026, 9, 7)
TOMORROW = date(2026, 9, 8)


async def press(hass: HomeAssistant) -> None:
    """Press the refresh button the way the UI and an automation both do."""
    await hass.services.async_call(
        Platform.BUTTON, "press", {"entity_id": REFRESH}, blocking=True
    )
    await hass.async_block_till_done()


async def test_pressing_fetches_and_reprocesses(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A press fetches now and the new curve is visible immediately.

    Publishing tomorrow *after* setup is the real case this exists for: the
    supplier released the prices between two fetch times and the user does not
    want to wait for the afternoon anchor.
    """
    await set_time_zone(hass)
    api.publish(TODAY, day_slots(TODAY, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 10))
    await setup_integration(hass, make_entry())

    assert hass.states.get(AVG_TOMORROW).state in ("unknown", "unavailable")
    calls_before = api.call_count

    api.publish(TOMORROW, day_slots(TOMORROW, [0.40] * 24))
    await press(hass)

    assert api.call_count == calls_before + 1
    # Not just fetched: re-rendered. The average is a pure function of the
    # cached curve, so a fetch that did not notify listeners would leave it
    # unknown even though the prices had arrived.
    assert float(hass.states.get(AVG_TOMORROW).state) == pytest.approx(0.40)
    assert hass.states.get(CURRENT_PRICE).attributes["tomorrow_valid"] is True


async def test_every_press_reaches_the_api(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Two presses are two fetches: the debouncer must not swallow either.

    async_request_refresh would merge presses that land close together, which
    is the right thing for a scheduled retry and the wrong thing for a button
    someone is pressing because they want an answer.
    """
    await set_time_zone(hass)
    api.publish(TODAY, day_slots(TODAY, [0.20] * 24))
    api.publish(TOMORROW, day_slots(TOMORROW, [0.30] * 24))

    freezer.move_to(at(2026, 9, 7, 10))
    await setup_integration(hass, make_entry())

    calls_before = api.call_count
    await press(hass)
    await press(hass)
    assert api.call_count == calls_before + 2


async def test_pressing_works_after_the_backoff_gave_up(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Once the retry ladder has given up, the button is the way back.

    The ladder stops after BACKOFF_MAX_TRIES and waits for the next daily
    anchor, which can be many hours away. A press restarts it from the top, the
    same as an anchor does, so fixing the cause does not mean waiting.
    """
    await set_time_zone(hass)
    api.publish(TODAY, day_slots(TODAY, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 6))
    await setup_integration(hass, make_entry())

    api.error = ClientError("boom")
    # Burn the whole ladder: 1 + 2 + 5 + 10 + 30 minutes and then silence.
    await advance(hass, freezer, timedelta(hours=2))
    exhausted = api.call_count
    assert exhausted >= BACKOFF_MAX_TRIES

    # Confirms the ladder really is spent, not merely slow.
    await advance(hass, freezer, timedelta(hours=2))
    assert api.call_count == exhausted

    api.error = None
    await press(hass)
    assert api.call_count == exhausted + 1
    assert float(hass.states.get(CURRENT_PRICE).state) == pytest.approx(0.20)


async def test_a_failed_press_is_reported_not_swallowed(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A press against a dead API must not look like a successful one.

    The coordinator deliberately absorbs fetch failures so cached prices keep
    being served. On a deliberate press that silence would be a lie, so the
    button raises and the UI shows it.
    """
    await set_time_zone(hass)
    api.publish(TODAY, day_slots(TODAY, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 10))
    await setup_integration(hass, make_entry())

    api.error = ClientError("the api is down")
    with pytest.raises(HomeAssistantError) as caught:
        await press(hass)
    assert "the api is down" in str(caught.value)

    # And the cached price survived the failed press: a refresh that cannot
    # reach the API must not cost the prices already held.
    assert float(hass.states.get(CURRENT_PRICE).state) == pytest.approx(0.20)


async def test_a_partial_failure_does_not_raise(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """One broken tariff type is not a failed refresh.

    The other types were genuinely refreshed, and the type on cache says so
    itself through from_cache. Raising here would report a working integration
    as broken every time one endpoint hiccups.
    """
    await set_time_zone(hass)
    api.publish(TODAY, day_slots(TODAY, [0.20] * 24))
    api.publish(TODAY, day_slots(TODAY, [0.05] * 24, tariff_type="grid"), tariff_type="grid")

    freezer.move_to(at(2026, 9, 7, 10))
    await setup_integration(hass, make_entry(tariff_types=["electricity", "grid"]))

    api.error = ClientError("grid is down")
    api.failing_types = {"grid"}
    await press(hass)

    assert hass.states.get(CURRENT_PRICE).attributes["from_cache"] is False
    grid = hass.states.get("sensor.test_grid_current_price")
    assert grid.attributes["from_cache"] is True
    assert float(grid.state) == pytest.approx(0.05)


async def test_the_button_stays_available_when_everything_else_failed(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A greyed-out refresh button would offer no way out of an outage."""
    await set_time_zone(hass)
    api.publish(TODAY, day_slots(TODAY, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 6))
    await setup_integration(hass, make_entry())

    api.error = ClientError("boom")
    await advance(hass, freezer, timedelta(hours=2))

    coordinator = next(iter(hass.data[DOMAIN].values()))
    assert coordinator.failed_tariff_types() == {"electricity": "boom"}
    assert hass.states.get(REFRESH).state != "unavailable"


async def test_the_failure_check_cannot_use_last_update_success(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Why the button judges per tariff type, demonstrated.

    Carrying the cached curves forward is a success as far as the coordinator
    is concerned, even in a cycle where every single type failed. A button that
    checked last_update_success would therefore report a total outage as a
    successful refresh - so this pins both halves at once.
    """
    await set_time_zone(hass)
    api.publish(TODAY, day_slots(TODAY, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 10))
    await setup_integration(hass, make_entry())

    api.error = ClientError("total outage")
    with pytest.raises(HomeAssistantError):
        await press(hass)

    coordinator = next(iter(hass.data[DOMAIN].values()))
    assert coordinator.last_update_success is True
    assert coordinator.failed_tariff_types() == {"electricity": "total outage"}


async def test_the_button_belongs_to_the_entry_not_to_a_tariff_type(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """One button however many tariff types, on the integration's own device.

    A refresh always fetches every configured type in the same cycle, so a
    button per type would be five ways to do the identical thing.
    """
    await set_time_zone(hass)
    api.publish(TODAY, day_slots(TODAY, [0.20] * 24))
    api.publish(TODAY, day_slots(TODAY, [0.05] * 24, tariff_type="grid"), tariff_type="grid")

    freezer.move_to(at(2026, 9, 7, 10))
    entry = make_entry(tariff_types=["electricity", "grid"])
    await setup_integration(hass, entry)

    buttons = [
        entity_id
        for entity_id in hass.states.async_entity_ids()
        if entity_id.startswith("button.")
    ]
    assert buttons == [REFRESH]

    registry = er.async_get(hass)
    item = registry.async_get(REFRESH)
    assert item is not None
    assert item.unique_id == f"{entry.entry_id}_refresh"
    # The same device as the price sensors, not a second one.
    assert item.device_id == registry.async_get(CURRENT_PRICE).device_id


async def test_one_press_refreshes_every_configured_tariff_type(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """The single button has to cover all of them, or it is not enough."""
    await set_time_zone(hass)
    api.publish(TODAY, day_slots(TODAY, [0.20] * 24))
    api.publish(TODAY, day_slots(TODAY, [0.05] * 24, tariff_type="grid"), tariff_type="grid")

    freezer.move_to(at(2026, 9, 7, 10))
    await setup_integration(hass, make_entry(tariff_types=["electricity", "grid"]))

    api.calls.clear()
    await press(hass)

    assert {call["tariff_type"] for call in api.calls} == {"electricity", "grid"}
