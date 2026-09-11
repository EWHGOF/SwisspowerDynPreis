"""Tests for the diagnostic raw API response sensor.

The one thing this entity has to get right is that it shows what the API sent,
not what the integration made of it. If it showed the processed curve it would
answer the question "did the API really send this?" with a confident yes, every
time, which is worse than not existing.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.swisspower_dynpreis.sensor import (
    SwisspowerDynPreisRawResponseSensor,
)

from .helpers import (
    CURRENT_PRICE,
    FakeApi,
    advance,
    all_entities_enabled,
    at,
    day_slots,
    make_entry,
    set_time_zone,
    setup_integration,
)

RAW = "sensor.test_api_response"

TODAY = date(2026, 9, 7)


def unterminated_slots(day: date, value: float = 0.20) -> list[dict[str, Any]]:
    """24 hourly slots the way an API that omits the end of a slot sends them.

    The normalizer fills the missing end in from the next slot's start, which is
    exactly the kind of processing this sensor exists to show the other side of.
    """
    return [
        {
            "start_timestamp": at(day.year, day.month, day.day, hour).isoformat(),
            "electricity": [
                {"component": "energy", "unit": "CHF/kWh", "value": value}
            ],
        }
        for hour in range(24)
    ]


async def test_it_is_registered_but_off_by_default(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Off by default, because the payload is large - but there to switch on."""
    await set_time_zone(hass)
    api.publish(TODAY, day_slots(TODAY, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 10))
    await setup_integration(hass, make_entry())

    assert hass.states.get(RAW) is None

    registry = er.async_get(hass)
    item = registry.async_get(RAW)
    assert item is not None, "it has to be registered, or nobody can enable it"
    assert item.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert item.entity_category is EntityCategory.DIAGNOSTIC


async def test_it_holds_the_response_before_processing(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """The whole point: raw in, not the normalized curve back out.

    The published slots carry no end_timestamp. The integration fills one in
    from the next slot's start - so if this attribute has one, it is showing the
    processed data and the entity is worthless.
    """
    await set_time_zone(hass)
    api.publish(TODAY, unterminated_slots(TODAY))

    freezer.move_to(at(2026, 9, 7, 10))
    with all_entities_enabled():
        await setup_integration(hass, make_entry())

    raw = hass.states.get(RAW).attributes["responses"]["electricity"]
    assert [slot.get("end_timestamp") for slot in raw["prices"]] == [None] * 24

    # And the processed side really did fill them in, so the two differ for the
    # reason claimed rather than because nothing happens at all.
    processed = hass.states.get(CURRENT_PRICE).attributes["prices"]
    assert all(slot.get("end_timestamp") for slot in processed)


async def test_the_state_is_the_payload_size(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A state is capped at 255 characters, so the payload cannot be the state.

    The size is the useful summary instead: an API that answered with nothing is
    visible at a glance.
    """
    await set_time_zone(hass)
    api.publish(TODAY, day_slots(TODAY, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 10))
    with all_entities_enabled():
        await setup_integration(hass, make_entry())

    state = hass.states.get(RAW)
    assert int(state.state) == state.attributes["bytes"]["electricity"]
    assert int(state.state) > 0
    assert state.attributes["captured"]["electricity"].startswith("2026-09-07T10:00")


async def test_every_tariff_type_is_kept_separately(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """One entity, one attribute, every configured type - each its own request."""
    await set_time_zone(hass)
    api.publish(TODAY, day_slots(TODAY, [0.20] * 24))
    api.publish(
        TODAY, day_slots(TODAY, [0.05] * 24, tariff_type="grid"), tariff_type="grid"
    )

    freezer.move_to(at(2026, 9, 7, 10))
    with all_entities_enabled():
        await setup_integration(hass, make_entry(tariff_types=["electricity", "grid"]))

    responses = hass.states.get(RAW).attributes["responses"]
    assert set(responses) == {"electricity", "grid"}
    assert responses["grid"]["prices"][0]["grid"][0]["value"] == 0.05
    assert int(hass.states.get(RAW).state) == sum(
        hass.states.get(RAW).attributes["bytes"].values()
    )


async def test_an_answer_the_integration_rejects_is_still_captured(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """The case this entity is really for.

    An API that answers 200 with an empty list is a success as far as the
    coordinator is concerned, and every price entity just goes quiet. The raw
    capture is what shows that the answer arrived and was empty, rather than
    leaving "no prices" and "no answer" looking identical.
    """
    await set_time_zone(hass)
    # Nothing published: the fake answers with an empty price list.

    freezer.move_to(at(2026, 9, 7, 10))
    with all_entities_enabled():
        await setup_integration(hass, make_entry())

    responses = hass.states.get(RAW).attributes["responses"]
    assert responses["electricity"] == {"prices": []}
    assert hass.states.get(RAW).attributes["captured"]["electricity"] is not None


async def test_a_later_fetch_replaces_the_captured_payload(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """It is a window onto the last exchange, not a log - and it has to move.

    Guards the render-skipping optimization below from freezing the entity: a
    fetch that brings something new must still get through.
    """
    await set_time_zone(hass)
    api.publish(TODAY, day_slots(TODAY, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 5, 55))
    with all_entities_enabled():
        await setup_integration(hass, make_entry(update_time="06:00"))

    first = hass.states.get(RAW).attributes["responses"]["electricity"]
    assert first["prices"][0]["electricity"][0]["value"] == 0.20

    api.unpublish(TODAY)
    api.publish(TODAY, day_slots(TODAY, [0.44] * 24))
    await advance(hass, freezer, timedelta(minutes=10))

    second = hass.states.get(RAW).attributes["responses"]["electricity"]
    assert second["prices"][0]["electricity"][0]["value"] == 0.44


async def test_it_is_not_rewritten_at_every_price_boundary(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """The payload changes twice a day; the render clock ticks 24 to 96 times.

    Without the guard this entity - the largest attribute payload in the
    integration - would be rebuilt and re-compared on every one of those ticks.
    """
    await set_time_zone(hass)
    api.publish(TODAY, day_slots(TODAY, [0.20] * 24))
    api.publish(
        date(2026, 9, 8), day_slots(date(2026, 9, 8), [0.30] * 24)
    )

    freezer.move_to(at(2026, 9, 7, 10))
    with all_entities_enabled():
        await setup_integration(hass, make_entry(update_time="06:00"))

    writes: list[None] = []
    original = SwisspowerDynPreisRawResponseSensor.async_write_ha_state

    def counted(self) -> None:
        writes.append(None)
        original(self)

    with patch.object(
        SwisspowerDynPreisRawResponseSensor, "async_write_ha_state", counted
    ):
        # Three hourly boundaries, no fetch in between: both days are published,
        # so nothing is being hunted for.
        calls_before = api.call_count
        await advance(hass, freezer, timedelta(hours=3), step=timedelta(minutes=15))
        assert api.call_count == calls_before, "the test needs a fetch-free stretch"
        assert writes == [], "the render clock must not rewrite the raw payload"


def test_the_payload_is_kept_out_of_the_recorder() -> None:
    """Tens of kilobytes, twice a day, for ever - and useless as history.

    The recorder refuses attributes over 16 KB and warns about it every time, so
    leaving these in would both bloat the database and fill the log.
    """
    declared = SwisspowerDynPreisRawResponseSensor._unrecorded_attributes
    assert declared >= {"responses", "captured", "bytes"}


@pytest.mark.parametrize("attribute", ["responses", "captured", "bytes"])
async def test_the_attributes_survive_a_failed_fetch(
    hass: HomeAssistant,
    api: FakeApi,
    freezer: FrozenDateTimeFactory,
    attribute: str,
) -> None:
    """A type whose fetch failed keeps the last answer that arrived.

    Dropping it would take the evidence away at the moment it is wanted, and the
    timestamp is there to say how old what is shown actually is.
    """
    from aiohttp import ClientError

    await set_time_zone(hass)
    api.publish(TODAY, day_slots(TODAY, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 5, 55))
    with all_entities_enabled():
        await setup_integration(hass, make_entry(update_time="06:00"))

    before = hass.states.get(RAW).attributes[attribute]

    api.error = ClientError("boom")
    await advance(hass, freezer, timedelta(minutes=30))

    assert hass.states.get(RAW).attributes[attribute] == before
