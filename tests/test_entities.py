"""Tests that the integration actually creates the entities it promises."""

from __future__ import annotations

from datetime import date

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.swisspower_dynpreis.sensor import STAT_SENSORS

from .helpers import (
    FakeApi,
    at,
    day_slots,
    make_entry,
    set_time_zone,
    setup_integration,
)


def _expected_entity_id(name: str) -> str:
    """Mirror Home Assistant's slugify for the entity names this platform builds."""
    slug = name.lower()
    for char in " %/-":
        slug = slug.replace(char, "_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return f"sensor.test_electricity_{slug.strip('_')}"


async def test_all_stat_entities_are_added(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Every STAT_SENSORS description must result in a real entity.

    Regression test: the stat entities used to be handed their description via
    ``self.entity_description``. Home Assistant reads its own fields off that
    attribute, and _StatSensorDescription does not have them, so all of these
    entities failed to be added with an AttributeError and silently never
    appeared - only the "Current price" sensors did.
    """
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 6))
    await setup_integration(hass, make_entry())

    entity_ids = set(hass.states.async_entity_ids())

    # The plain and the per-component current price sensor.
    assert "sensor.test_electricity_current_price" in entity_ids
    assert "sensor.test_electricity_energy_current_price" in entity_ids

    missing = [
        (description.key, _expected_entity_id(description.name))
        for description in STAT_SENSORS
        if _expected_entity_id(description.name) not in entity_ids
    ]
    assert not missing, f"stat entities missing: {missing}"
    assert len(entity_ids) == len(STAT_SENSORS) + 2, sorted(entity_ids)


async def test_no_errors_logged_while_adding_entities(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory, caplog
) -> None:
    """Adding the platform must not log any error."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 6))
    await setup_integration(hass, make_entry())

    assert "Error adding entity" not in caplog.text
    assert "AttributeError" not in caplog.text
