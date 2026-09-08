"""Tests for the fetch schedule: how often the API is hit, and when it is not."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from aiohttp import ClientError, ClientResponseError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.swisspower_dynpreis.const import (
    BACKOFF_MAX_TRIES,
    CONF_QUERY_YEAR,
    CONF_UPDATE_TIME,
    CONF_UPDATE_TIME_PM,
    DOMAIN,
    HUNT_MINUTES,
    HUNT_STOP_HOUR,
    MAX_RETRY_AFTER_SECONDS,
    TODAY_HUNT_MINUTES,
)

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


async def test_healthy_day_hits_the_api_twice(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """With both days published, only the two daily anchors fetch."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))
    api.publish(date(2026, 9, 8), day_slots(date(2026, 9, 8), [0.30] * 24))

    freezer.move_to(at(2026, 9, 7, 0, 30))
    await setup_integration(hass, make_entry(update_time="06:00"))
    calls_after_setup = api.call_count

    # Through the whole day: the 06:00 and 14:00 anchors, nothing else.
    await advance(hass, freezer, timedelta(hours=23), step=timedelta(minutes=10))

    assert api.call_count - calls_after_setup == 2, [
        str(call["start"]) for call in api.calls
    ]


async def test_no_polling_before_the_afternoon_anchor(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Tomorrow does not exist in the morning, so nothing is gained by asking."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 6, 30))
    await setup_integration(hass, make_entry(update_time="06:00"))
    calls_after_setup = api.call_count

    await advance(hass, freezer, timedelta(hours=7), step=timedelta(minutes=10))

    assert api.call_count == calls_after_setup, (
        "must not poll for tomorrow before the afternoon anchor"
    )


async def test_hunting_stops_once_tomorrow_arrives(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Once tomorrow is complete the extra polling has to stop."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    tomorrow = date(2026, 9, 8)
    api.publish(today, day_slots(today, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 13, 30))
    await setup_integration(hass, make_entry(update_time="06:00"))

    # The afternoon anchor fires, tomorrow is still missing, hunting begins.
    api.publish(tomorrow, day_slots(tomorrow, [0.30] * 24))
    await advance(hass, freezer, timedelta(hours=2), step=timedelta(minutes=10))
    assert float(hass.states.get(AVG_TOMORROW).state) == pytest.approx(0.30)

    calls_when_complete = api.call_count
    await advance(hass, freezer, timedelta(hours=5), step=timedelta(minutes=10))

    assert api.call_count == calls_when_complete, (
        "hunting must stop once tomorrow is published"
    )


async def test_hunting_is_bounded_when_tomorrow_never_arrives(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A day where tomorrow is never published must not hammer the API."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 13, 30))
    await setup_integration(hass, make_entry(update_time="06:00"))
    calls_after_setup = api.call_count

    await advance(hass, freezer, timedelta(hours=10), step=timedelta(minutes=5))

    hunts = api.call_count - calls_after_setup
    # One afternoon anchor plus at most the escalating hunt schedule.
    assert hunts <= 1 + len(HUNT_MINUTES), hunts
    assert hunts >= 2, "the afternoon anchor plus at least one hunt must have run"


async def test_no_requests_after_the_hunt_stop_hour(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Late in the evening the integration goes quiet until the next anchor."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, HUNT_STOP_HOUR, 5))
    await setup_integration(hass, make_entry(update_time="06:00"))
    calls_after_setup = api.call_count

    await advance(hass, freezer, timedelta(minutes=50), step=timedelta(minutes=5))

    assert api.call_count == calls_after_setup, (
        f"must not fetch after {HUNT_STOP_HOUR}:00 local"
    )


async def test_backoff_gives_up_and_waits_for_the_next_anchor(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A dead API must produce a bounded number of retries, not a stream."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 5, 55))
    await setup_integration(hass, make_entry(update_time="06:00"))
    calls_after_setup = api.call_count

    api.error = ClientError("dead")
    # Long enough to exhaust the chain, but short of the afternoon anchor.
    await advance(hass, freezer, timedelta(hours=6), step=timedelta(minutes=5))

    attempts = api.call_count - calls_after_setup
    assert attempts <= 1 + BACKOFF_MAX_TRIES, attempts
    assert attempts >= 2, "the anchor plus at least one backoff retry must have run"


async def test_partial_tomorrow_counts_as_incomplete(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Only the first few hours of tomorrow must not end the hunt."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    tomorrow = date(2026, 9, 8)
    api.publish(today, day_slots(today, [0.20] * 24))
    api.publish(tomorrow, day_slots(tomorrow, [0.30] * 6))

    freezer.move_to(at(2026, 9, 7, 13, 30))
    await setup_integration(hass, make_entry(update_time="06:00"))

    coordinator = hass.data[DOMAIN][entry_id(hass)]
    assert not coordinator.tomorrow_complete(coordinator.data)

    # Now the rest arrives.
    api.publish(tomorrow, day_slots(tomorrow, [0.30] * 24))
    await advance(hass, freezer, timedelta(hours=2), step=timedelta(minutes=10))

    assert coordinator.tomorrow_complete(coordinator.data)


async def test_query_year_uses_anchors_only_and_never_schedules_the_past(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """The test-year override must not hunt, and must not arm a past timer.

    With the reference year rewritten, every cached boundary lies in another
    year. A render target in the past would fire immediately and spin the
    event loop, so the next target must always be in the future.
    """
    await set_time_zone(hass)
    api.publish(date(2024, 9, 7), day_slots(date(2024, 9, 7), [0.20] * 24))

    entry = make_entry(update_time="06:00", options={CONF_QUERY_YEAR: "2024"})

    freezer.move_to(at(2026, 9, 7, 13, 30))
    await setup_integration(hass, entry)

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.update_interval is None, "the test year must not hunt"

    now = dt_util.now()
    target = coordinator._next_render_instant(now, coordinator.data)
    assert target > now, "the render timer must never be armed in the past"

    # 13:30 to 21:30 crosses the afternoon anchor, which must still fire once -
    # but nothing beyond it, because tomorrow will never look complete in a
    # rewritten year and hunting would otherwise run to its cap every day.
    calls_after_setup = api.call_count
    await advance(hass, freezer, timedelta(hours=8), step=timedelta(minutes=10))
    assert api.call_count == calls_after_setup + 1


async def test_render_target_is_the_next_slot_start_then_midnight(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """The render instant comes from the data and is capped at local midnight."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(
        today,
        day_slots(today, [0.10 + i * 0.001 for i in range(96)], slot_minutes=15),
    )

    freezer.move_to(at(2026, 9, 7, 6, 5))
    entry = make_entry()
    await setup_integration(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    target = coordinator._next_render_instant(dt_util.now(), coordinator.data)
    assert dt_util.as_local(target) == at(2026, 9, 7, 6, 15)

    # With no data at all, the cap is the next local midnight.
    empty_target = coordinator._next_render_instant(dt_util.now(), {})
    assert dt_util.as_local(empty_target) == at(2026, 9, 8, 0, 0)


async def test_afternoon_anchor_is_configurable(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """The afternoon anchor honours its option instead of a hard-coded time."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    entry = make_entry(update_time="06:00", options={CONF_UPDATE_TIME_PM: "16:30"})

    freezer.move_to(at(2026, 9, 7, 15, 0))
    await setup_integration(hass, entry)
    calls_after_setup = api.call_count

    await advance(hass, freezer, timedelta(minutes=60), step=timedelta(minutes=5))
    assert api.call_count == calls_after_setup, "must stay quiet before 16:30"

    await advance(hass, freezer, timedelta(minutes=45), step=timedelta(minutes=5))
    assert api.call_count > calls_after_setup, "the 16:30 anchor must fire"


async def test_empty_morning_anchor_keeps_the_afternoon_anchor(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """Clearing the morning time must not take the afternoon one with it."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    entry = make_entry(update_time=None, options={CONF_UPDATE_TIME: ""})

    freezer.move_to(at(2026, 9, 7, 13, 30))
    await setup_integration(hass, entry)

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator._update_time is None
    assert coordinator._update_time_pm is not None

    calls_after_setup = api.call_count
    await advance(hass, freezer, timedelta(minutes=45), step=timedelta(minutes=5))
    assert api.call_count > calls_after_setup, "the afternoon anchor must still fire"


def entry_id(hass: HomeAssistant) -> str:
    """Return the only config entry's id."""
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    return entries[0].entry_id


async def test_timeout_is_retried_like_any_other_failure(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A request timeout must schedule a retry, not silently stop.

    api.py wraps every request in a timeout, and DataUpdateCoordinator handles
    a bare TimeoutError itself - so unless the coordinator catches it, no
    backoff would ever be scheduled for the most likely failure of all.
    """
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 5, 55))
    entry = make_entry(update_time="06:00")
    await setup_integration(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    api.error = TimeoutError()
    await advance(hass, freezer, timedelta(minutes=20), step=timedelta(minutes=5))

    assert coordinator.last_update_success is False
    assert coordinator.update_interval is not None, (
        "a timeout must schedule a backoff retry"
    )
    calls_after_timeout = api.call_count

    api.error = None
    await advance(hass, freezer, timedelta(minutes=40), step=timedelta(minutes=5))
    assert api.call_count > calls_after_timeout, "the timeout must be retried"


async def test_backoff_ladder_restarts_at_the_next_anchor(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A day of failures must not disable retries for good.

    The failure counter used to reset only on success, so once the chain gave
    up after BACKOFF_MAX_TRIES the backoff returned None for ever: from the
    next day on there was exactly one attempt per day and no retries at all.
    Every anchor has to start a fresh ladder.
    """
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 5, 55))
    entry = make_entry(update_time="06:00")
    await setup_integration(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # A whole day of failures exhausts the ladder.
    api.error = ClientError("dead")
    await advance(hass, freezer, timedelta(hours=18), step=timedelta(minutes=10))
    assert coordinator.update_interval is None, "the chain must have given up"

    # Next day, still dead: the morning anchor must retry more than once.
    calls_day_one = api.call_count
    await advance(hass, freezer, timedelta(hours=12), step=timedelta(minutes=10))

    attempts_day_two = api.call_count - calls_day_one
    assert attempts_day_two > 2, (
        f"the ladder must restart at the next anchor, got {attempts_day_two} "
        "attempts on day two"
    )


async def test_missing_today_is_chased_without_waiting_for_the_afternoon(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """An empty but successful response must not park the integration.

    The API answering 200 with no slots is not a failure, so the failure
    backoff never sees it. Without its own ladder the integration would sit
    with every entity unknown until the afternoon anchor hours later.
    """
    await set_time_zone(hass)
    today = date(2026, 9, 7)

    # Nothing published yet: setup succeeds but there is no price to show.
    freezer.move_to(at(2026, 9, 7, 7, 0))
    entry = make_entry(update_time="06:00")
    await setup_integration(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    assert hass.states.get(CURRENT_PRICE).state == "unknown"
    assert coordinator.update_interval is not None, (
        "a missing current price must schedule another attempt"
    )

    # Today's prices appear a little later and must be picked up quickly.
    api.publish(today, day_slots(today, [0.20] * 24))
    await advance(hass, freezer, timedelta(minutes=30), step=timedelta(minutes=5))

    assert float(hass.states.get(CURRENT_PRICE).state) == pytest.approx(0.20)
    assert coordinator.update_interval is None or coordinator._hunt_kind == "tomorrow"


async def test_missing_today_ladder_is_bounded(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """If nothing is ever published, the today ladder still has to end."""
    await set_time_zone(hass)

    freezer.move_to(at(2026, 9, 7, 7, 0))
    await setup_integration(hass, make_entry(update_time="06:00"))
    calls_after_setup = api.call_count

    await advance(hass, freezer, timedelta(hours=12), step=timedelta(minutes=5))

    attempts = api.call_count - calls_after_setup
    # The today ladder, plus the afternoon anchor which restarts it once.
    assert attempts <= 2 * len(TODAY_HUNT_MINUTES) + 1, attempts


def _response_error(status: int, headers: dict[str, str] | None = None) -> ClientResponseError:
    """Build the error aiohttp's raise_for_status would raise."""
    return ClientResponseError(
        request_info=None,
        history=(),
        status=status,
        message=f"HTTP {status}",
        headers=headers,
    )


async def test_rejected_credentials_are_not_retried_in_a_ladder(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A permanent 4xx must wait for the next anchor, not ladder.

    api.py calls raise_for_status, so a wrong token or tariff name arrives as a
    ClientResponseError exactly like a network blip. Laddering it means politely
    hammering the API with a credential that will never work.
    """
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 5, 55))
    entry = make_entry(update_time="06:00")
    await setup_integration(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    api.error = _response_error(403)
    await advance(hass, freezer, timedelta(minutes=20), step=timedelta(minutes=5))

    assert coordinator.last_update_success is False
    assert coordinator.update_interval is None, "a rejected request must not ladder"

    calls_after_rejection = api.call_count
    await advance(hass, freezer, timedelta(hours=6), step=timedelta(minutes=10))
    assert api.call_count == calls_after_rejection, (
        "nothing may be retried until the next anchor"
    )


async def test_server_errors_still_ladder(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A 5xx is transient and must keep the escalating retry."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 5, 55))
    entry = make_entry(update_time="06:00")
    await setup_integration(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    api.error = _response_error(503)
    await advance(hass, freezer, timedelta(minutes=20), step=timedelta(minutes=5))

    assert coordinator.update_interval is not None, "a 5xx must be retried"


async def test_retry_after_header_is_honoured(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A 429 with Retry-After must set that wait instead of the ladder."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 5, 55))
    entry = make_entry(update_time="06:00")
    await setup_integration(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    api.error = _response_error(429, {"Retry-After": "900"})
    await advance(hass, freezer, timedelta(minutes=20), step=timedelta(minutes=5))

    assert coordinator.update_interval == timedelta(minutes=15), (
        coordinator.update_interval
    )


async def test_absurd_retry_after_is_clamped(
    hass: HomeAssistant, api: FakeApi, freezer: FrozenDateTimeFactory
) -> None:
    """A broken Retry-After must not park the integration for days."""
    await set_time_zone(hass)
    today = date(2026, 9, 7)
    api.publish(today, day_slots(today, [0.20] * 24))

    freezer.move_to(at(2026, 9, 7, 5, 55))
    entry = make_entry(update_time="06:00")
    await setup_integration(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    api.error = _response_error(429, {"Retry-After": "9999999"})
    await advance(hass, freezer, timedelta(minutes=20), step=timedelta(minutes=5))

    assert coordinator.update_interval == timedelta(seconds=MAX_RETRY_AFTER_SECONDS)
