"""Tests that credentials never reach the log."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone

import pytest
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.swisspower_dynpreis.api import redact_url
from custom_components.swisspower_dynpreis.const import (
    CONF_METERING_CODE,
    CONF_METHOD,
    CONF_TARIFF_TYPES,
    CONF_TOKEN,
    DOMAIN,
    METHOD_METERING_CODE,
    WINDOW_DAYS_FORWARD,
)

from .helpers import FakeApi, at, day_slots, set_time_zone, setup_integration

TOKEN = "sk-live-9c1f4b7e-never-log-me"
METERING_CODE = "CH1002401234500000000000000012345"


def test_redact_url_masks_the_metering_code() -> None:
    url = (
        "https://esit.code-fabrik.ch/api/v1/metering_code"
        f"?tariff_type=electricity&metering_code={METERING_CODE}&start_timestamp=x"
    )
    redacted = redact_url(url)
    assert METERING_CODE not in redacted
    assert "**REDACTED**" in redacted
    # The rest of the URL is still useful for debugging.
    assert "tariff_type=electricity" in redacted
    assert "start_timestamp=x" in redacted


def test_redact_url_leaves_a_tariff_name_url_alone() -> None:
    url = "https://esit.code-fabrik.ch/api/v1/tariff_name?tariff_name=D1"
    assert redact_url(url) == url


async def test_the_token_never_reaches_the_log(
    hass: HomeAssistant, api: FakeApi, freezer, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: the request headers were logged at info level.

    api.py logged "headers=%s" with the Authorization header in it, at info -
    so every user running the integration had a bearer token in their log by
    default, and in any log they attached to an issue report.
    """
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={
            CONF_NAME: "Test",
            CONF_METHOD: METHOD_METERING_CODE,
            CONF_METERING_CODE: METERING_CODE,
            CONF_TOKEN: TOKEN,
            CONF_TARIFF_TYPES: ["electricity"],
        },
        options={},
    )

    # Everything, including debug: a token must not appear even there.
    with caplog.at_level(logging.DEBUG):
        freezer.move_to(at(2026, 9, 7, 6, 0))
        await setup_integration(hass, entry)

    assert caplog.text, "nothing was logged at all, so this proves nothing"
    assert TOKEN not in caplog.text
    assert "Authorization" not in caplog.text
    assert "Bearer" not in caplog.text
    assert METERING_CODE not in caplog.text


class _FakeResponse:
    """Just enough of aiohttp's response for the client's happy path."""

    status = 200
    headers = {"Content-Type": "application/json"}

    async def text(self) -> str:
        return "[]"

    def raise_for_status(self) -> None:
        return None

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _RecordingSession:
    """Records what the client would have sent, without any socket."""

    def __init__(self) -> None:
        self.url: object | None = None
        self.headers: dict[str, str] = {}

    def get(self, url: object, headers: dict[str, str] | None = None) -> _FakeResponse:
        self.url = url
        self.headers = dict(headers or {})
        return _FakeResponse()


async def test_the_real_client_does_not_log_headers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exercise api.py itself, not the mocked-out client used elsewhere."""
    from custom_components.swisspower_dynpreis.api import SwisspowerDynPreisApiClient

    session = _RecordingSession()
    client = SwisspowerDynPreisApiClient(session, METHOD_METERING_CODE, TOKEN)

    with caplog.at_level(logging.DEBUG):
        await client.fetch_tariffs(
            tariff_type="electricity",
            start=datetime(2026, 9, 7, tzinfo=timezone.utc),
            end=datetime(2026, 9, 9, tzinfo=timezone.utc),
            metering_code=METERING_CODE,
        )

    # The token really is sent, and the metering code really is in the URL ...
    assert session.headers.get("Authorization") == f"Bearer {TOKEN}"
    assert METERING_CODE in str(session.url)

    # ... and neither is logged, at any level.
    assert caplog.text, "nothing was logged, so this proves nothing"
    assert TOKEN not in caplog.text
    assert "Bearer" not in caplog.text
    assert "Authorization" not in caplog.text
    assert METERING_CODE not in caplog.text


async def test_diagnostics_are_useful_and_redacted(
    hass: HomeAssistant, api: FakeApi, freezer
) -> None:
    """Diagnostics must explain the schedule without leaking credentials."""
    from custom_components.swisspower_dynpreis.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={
            CONF_NAME: "Test",
            CONF_METHOD: METHOD_METERING_CODE,
            CONF_METERING_CODE: METERING_CODE,
            CONF_TOKEN: TOKEN,
            CONF_TARIFF_TYPES: ["electricity"],
        },
        options={},
    )
    freezer.move_to(at(2026, 9, 7, 6, 0))
    await setup_integration(hass, entry)

    result = await async_get_config_entry_diagnostics(hass, entry)
    dumped = json.dumps(result)

    assert TOKEN not in dumped
    assert METERING_CODE not in dumped

    # And it actually answers the questions an issue report raises.
    assert result["schedule"]["covers_now"] is True
    assert result["schedule"]["tomorrow_complete"] is False
    # Including "why is the last day empty": the window reaches further than a
    # day-ahead supplier publishes, and the diagnostics has to say how far.
    assert result["schedule"]["window_days_forward"] == WINDOW_DAYS_FORWARD
    assert result["tariff_types"]["electricity"]["slot_count"] == 24
    assert result["tariff_types"]["electricity"]["serving_from_cache"] is False
    assert result["tariff_types"]["electricity"]["last_successful_fetch"] is not None
