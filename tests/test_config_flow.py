"""Tests for the setup flow, and for the fetch times it asks for."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.swisspower_dynpreis.const import (
    API_BASE,
    CONF_API_URL,
    CONF_METERING_CODE,
    CONF_METHOD,
    CONF_TARIFF_NAME,
    CONF_TARIFF_TYPES,
    CONF_TOKEN,
    CONF_UPDATE_TIME,
    CONF_UPDATE_TIME_EVENING,
    CONF_UPDATE_TIME_PM,
    DOMAIN,
    METHOD_METERING_CODE,
    METHOD_TARIFF_NAME,
)

from .helpers import (
    FakeApi,
    advance,
    at,
    day_slots,
    make_entry,
    set_time_zone,
    setup_integration,
)

TIME_KEYS = (CONF_UPDATE_TIME, CONF_UPDATE_TIME_PM, CONF_UPDATE_TIME_EVENING)


def _defaults(result) -> dict[str, str]:
    """Return the default each field of a form starts out with."""
    return {
        str(key): key.default()
        for key in result["data_schema"].schema
        if callable(getattr(key, "default", None))
    }


async def _start(hass: HomeAssistant, method: str):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_NAME: "Test", CONF_API_URL: API_BASE, CONF_METHOD: method},
    )


async def test_setup_asks_for_the_fetch_times_with_the_defaults(
    hass: HomeAssistant,
) -> None:
    """The last setup step offers three times, 01:00, 14:00 and 18:30."""
    result = await _start(hass, METHOD_TARIFF_NAME)
    assert result["step_id"] == "tariff_name"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_TARIFF_NAME: "D1", CONF_TARIFF_TYPES: ["electricity"]},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "schedule"
    assert _defaults(result) == {
        CONF_UPDATE_TIME: "01:00:00",
        CONF_UPDATE_TIME_PM: "14:00:00",
        CONF_UPDATE_TIME_EVENING: "18:30:00",
    }

    with patch(
        "custom_components.swisspower_dynpreis.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.data[CONF_TARIFF_NAME] == "D1"
    # The times belong in the options: that is where the coordinator reads
    # them and where the options flow edits them.
    assert not set(TIME_KEYS) & set(entry.data)
    assert entry.options == {
        CONF_UPDATE_TIME: "01:00:00",
        CONF_UPDATE_TIME_PM: "14:00:00",
        CONF_UPDATE_TIME_EVENING: "18:30:00",
    }


async def test_setup_stores_the_times_that_were_entered(
    hass: HomeAssistant,
) -> None:
    """The metering path reaches the same step, and keeps what was typed."""
    result = await _start(hass, METHOD_METERING_CODE)
    assert result["step_id"] == "metering"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_METERING_CODE: "CH123",
            CONF_TOKEN: "secret",
            CONF_TARIFF_TYPES: ["electricity"],
        },
    )
    assert result["step_id"] == "schedule"

    times = {
        CONF_UPDATE_TIME: "02:15:00",
        CONF_UPDATE_TIME_PM: "15:00:00",
        CONF_UPDATE_TIME_EVENING: "20:45:00",
    }
    with patch(
        "custom_components.swisspower_dynpreis.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=times
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.data[CONF_METERING_CODE] == "CH123"
    assert entry.data[CONF_TOKEN] == "secret"
    assert entry.options == times


async def test_options_offer_all_three_times(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """The options show the stored times back, the third one included."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 10, 30))
    entry = make_entry(
        update_time="01:00",
        options={CONF_UPDATE_TIME_EVENING: "19:15"},
    )
    await setup_integration(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = _defaults(result)
    assert defaults[CONF_UPDATE_TIME] == "01:00:00"
    assert defaults[CONF_UPDATE_TIME_PM] == "14:00:00"
    assert defaults[CONF_UPDATE_TIME_EVENING] == "19:15:00"


async def test_the_evening_anchor_honours_its_option(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """The third time fetches, at the time it is set to."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))
    api.publish(date(2026, 9, 8), day_slots(date(2026, 9, 8), [0.30] * 24))

    entry = make_entry(update_time="06:00", options={CONF_UPDATE_TIME_EVENING: "20:00"})

    freezer.move_to(at(2026, 9, 7, 18, 0))
    await setup_integration(hass, entry)
    calls_after_setup = api.call_count

    await advance(hass, freezer, timedelta(minutes=90), step=timedelta(minutes=5))
    assert api.call_count == calls_after_setup, "must stay quiet before 20:00"

    await advance(hass, freezer, timedelta(minutes=45), step=timedelta(minutes=5))
    assert api.call_count == calls_after_setup + 1, "the 20:00 anchor must fire"


async def test_the_same_time_twice_is_one_fetch(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Two fields on the same time must not send two requests at once."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))
    api.publish(date(2026, 9, 8), day_slots(date(2026, 9, 8), [0.30] * 24))

    entry = make_entry(
        update_time="06:00",
        options={CONF_UPDATE_TIME_PM: "18:30", CONF_UPDATE_TIME_EVENING: "18:30"},
    )

    freezer.move_to(at(2026, 9, 7, 0, 30))
    await setup_integration(hass, entry)
    calls_after_setup = api.call_count

    await advance(hass, freezer, timedelta(hours=23), step=timedelta(minutes=10))
    assert api.call_count - calls_after_setup == 2
