"""Tests that the integration creates the entities it promises, where it promises."""

from __future__ import annotations

from datetime import date

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.swisspower_dynpreis.binary_sensor import BINARY_DESCRIPTIONS
from custom_components.swisspower_dynpreis.const import DOMAIN
from custom_components.swisspower_dynpreis.sensor import SENSOR_DESCRIPTIONS

from .helpers import (
    FakeApi,
    all_entities_enabled,
    at,
    day_slots,
    make_entry,
    set_time_zone,
    setup_integration,
)


def _entity_id(domain: str, name: str) -> str:
    """Mirror Home Assistant's slugify for the names this integration builds."""
    slug = name.lower()
    for char in " %/-":
        slug = slug.replace(char, "_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return f"{domain}.test_electricity_{slug.strip('_')}"


async def test_all_entities_are_added_on_the_right_platform(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Numeric statistics belong to sensor, on/off ones to binary_sensor.

    Regression on two counts: the stat entities used to be handed their
    description via ``self.entity_description``, which made every one of them
    fail to be added; and the on/off ones used to be added through the sensor
    platform, which put them in the sensor domain with a state of "on".

    Set up with everything enabled: which entities a fresh install starts with
    is a separate question, pinned by the test below.
    """
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 6))
    with all_entities_enabled():
        await setup_integration(hass, make_entry())

    entity_ids = set(hass.states.async_entity_ids())

    # The plain and the per-component current price sensor.
    assert "sensor.test_electricity_current_price" in entity_ids
    assert "sensor.test_electricity_energy_current_price" in entity_ids

    missing = [
        (description.key, _entity_id("sensor", description.name))
        for description in SENSOR_DESCRIPTIONS
        if _entity_id("sensor", description.name) not in entity_ids
    ]
    assert not missing, f"sensor entities missing: {missing}"

    missing = [
        (description.key, _entity_id("binary_sensor", description.name))
        for description in BINARY_DESCRIPTIONS
        if _entity_id("binary_sensor", description.name) not in entity_ids
    ]
    assert not missing, f"binary sensor entities missing: {missing}"

    # Nothing on/off may be left behind in the sensor domain.
    strays = [
        _entity_id("sensor", description.name)
        for description in BINARY_DESCRIPTIONS
        if _entity_id("sensor", description.name) in entity_ids
    ]
    assert not strays, f"on/off entities still in the sensor domain: {strays}"

    # The two current price sensors - plain and per component - plus the one
    # refresh button, which belongs to the entry rather than to a tariff type.
    assert "button.test_refresh_now" in entity_ids
    assert len(entity_ids) == len(SENSOR_DESCRIPTIONS) + len(BINARY_DESCRIPTIONS) + 3


async def test_binary_sensors_report_on_and_off(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A binary sensor's state must be on/off, and it must be in its own domain."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 6))
    with all_entities_enabled():
        await setup_integration(hass, make_entry())

    state = hass.states.get("binary_sensor.test_electricity_cheapest_50_hours_today")
    assert state is not None
    assert state.state == "on"


def test_the_enabled_default_rule_is_a_numeric_state() -> None:
    """Enabled by default means "the state is a number", and nothing else.

    Asserted on the descriptions rather than on a list of keys so a new entity
    has to pick a side: a CHF/kWh sensor is on, anything else - the timestamp
    sensor, every on/off sensor - is off.
    """
    for description in SENSOR_DESCRIPTIONS:
        numeric = description.unit == "CHF/kWh"
        assert description.enabled_default is numeric, description.key

    for description in BINARY_DESCRIPTIONS:
        assert description.enabled_default is False, description.key


async def test_a_fresh_install_starts_with_the_numeric_entities_only(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Five tariff types' worth of on/off entities is clutter nobody asked for.

    The rest is registered but disabled, not missing: it stays one click away in
    the entity registry. Home Assistant reads the default at first registration
    only, so an existing install keeps whatever it has enabled today.
    """
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 6))
    entry = make_entry()
    await setup_integration(hass, entry)

    assert set(hass.states.async_entity_ids()) == {
        # The current price, plain and per component: both are numbers.
        "sensor.test_electricity_current_price",
        "sensor.test_electricity_energy_current_price",
        *(
            _entity_id("sensor", description.name)
            for description in SENSOR_DESCRIPTIONS
            if description.enabled_default
        ),
        # The numeric rule is about what the integration *measures*. The
        # refresh button is a control, and a control nobody can find is not
        # one, so it is on by default whatever its state looks like.
        "button.test_refresh_now",
    }

    registry = er.async_get(hass)
    for domain, descriptions in (
        ("sensor", SENSOR_DESCRIPTIONS),
        ("binary_sensor", BINARY_DESCRIPTIONS),
    ):
        for description in descriptions:
            if description.enabled_default:
                continue
            item = registry.async_get(_entity_id(domain, description.name))
            assert item is not None, description.key
            assert item.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_legacy_sensor_domain_rows_are_removed(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """An install from before the split must not keep orphaned sensor.* rows.

    Simulates the old layout by registering the on/off unique ids in the sensor
    domain before setup, then asserts setup cleaned them up rather than leaving
    them as unavailable entities the integration no longer provides.
    """
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    entry = make_entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)

    legacy_ids = []
    for description in BINARY_DESCRIPTIONS:
        legacy = registry.async_get_or_create(
            Platform.SENSOR,
            DOMAIN,
            f"{entry.entry_id}_electricity_{description.key}",
            suggested_object_id=f"legacy_{description.key}",
            config_entry=entry,
        )
        legacy_ids.append(legacy.entity_id)

    assert all(registry.async_get(eid) is not None for eid in legacy_ids)

    freezer.move_to(at(2026, 9, 7, 6))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for entity_id in legacy_ids:
        assert registry.async_get(entity_id) is None, f"{entity_id} was not cleaned up"

    # And the replacements exist in the right domain.
    assert (
        registry.async_get_entity_id(
            Platform.BINARY_SENSOR,
            DOMAIN,
            f"{entry.entry_id}_electricity_{BINARY_DESCRIPTIONS[0].key}",
        )
        is not None
    )


async def test_no_errors_logged_while_adding_entities(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory, caplog
) -> None:
    """Adding either platform must not log an error."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 6))
    await setup_integration(hass, make_entry())

    assert "Error adding entity" not in caplog.text
    assert "AttributeError" not in caplog.text
