"""Data coordinator for Swisspower DynPreis.

Two clocks drive this integration and they are deliberately separate:

* A fetch clock decides when to talk to the API. It is anchored on the two
  configured daily times, escalates after a failure, and keeps looking for
  tomorrow's prices once the afternoon anchor has passed.
* A render clock decides when to recompute entity state from the price curve
  that is already cached locally. Nearly every entity value is a function of
  (cached slots, now), so it has to be re-rendered whenever the clock crosses
  a slot boundary or local midnight - without any network traffic.
"""

from __future__ import annotations

import inspect
import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_time_change,
)
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .api import SwisspowerDynPreisApiClient
from .const import (
    API_BASE,
    BACKOFF_MAX_TRIES,
    BACKOFF_MINUTES,
    CONF_API_URL,
    CONF_METERING_CODE,
    CONF_METHOD,
    CONF_QUERY_YEAR,
    CONF_TARIFF_NAME,
    CONF_TARIFF_TYPES,
    CONF_TOKEN,
    CONF_UPDATE_TIME,
    CONF_UPDATE_TIME_PM,
    DEFAULT_UPDATE_TIME,
    DEFAULT_UPDATE_TIME_PM,
    DOMAIN,
    HUNT_MINUTES,
    HUNT_STOP_HOUR,
    MAX_RETRY_AFTER_SECONDS,
    TODAY_HUNT_MINUTES,
    TOMORROW_COVERAGE_RATIO,
    WINDOW_DAYS_FORWARD,
)
from .pricing import find_current_slot, slot_bounds

_LOGGER = logging.getLogger(__name__)


class SwisspowerDynPreisCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for Swisspower DynPreis."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Set up the coordinator and arm its timers."""
        entry_data = entry.data
        options = entry.options

        self._entry = entry
        self._method = entry_data[CONF_METHOD]
        self._api_url = entry_data.get(CONF_API_URL, API_BASE)
        self._metering_code = entry_data.get(CONF_METERING_CODE)
        self._tariff_name = entry_data.get(CONF_TARIFF_NAME)
        self._tariff_types = entry_data[CONF_TARIFF_TYPES]
        self._token = entry_data.get(CONF_TOKEN)
        self._update_time = _coerce_time(options.get(CONF_UPDATE_TIME, DEFAULT_UPDATE_TIME))
        self._update_time_pm = _coerce_time(
            options.get(CONF_UPDATE_TIME_PM, DEFAULT_UPDATE_TIME_PM)
        )
        self._query_year = _coerce_year(options.get(CONF_QUERY_YEAR))

        self._fail_count = 0
        self._hunt_count = 0
        self._hunt_day: date | None = None
        self._hunt_kind: str | None = None
        self._render_unsub = None
        self._tearing_down = False

        session = async_get_clientsession(hass)
        self._client = SwisspowerDynPreisApiClient(
            session,
            self._method,
            self._token,
            api_base=self._api_url,
        )

        # No fixed interval: the daily anchors below drive the healthy case, and
        # _async_update_data sets a real interval only while retrying a failure
        # or still waiting for tomorrow's prices.
        #
        # always_update=False: the base class notifies every listener after each
        # refresh whether or not the payload changed. A hunt cycle that returns
        # the same curve would otherwise re-render every entity for nothing -
        # time-driven updates are the render clock's job, not the fetch's.
        kwargs: dict[str, Any] = {
            "name": DOMAIN,
            "update_interval": None,
            "always_update": False,
        }
        # The config_entry keyword was added after 2024.3, where the base class
        # instead reads the current_entry ContextVar. Passing it where it exists
        # removes that dependency - self.config_entry is what carries the base
        # teardown registration and pref_disable_polling - and silences the
        # deprecation current versions report.
        if "config_entry" in inspect.signature(DataUpdateCoordinator.__init__).parameters:
            kwargs["config_entry"] = entry
        super().__init__(hass, _LOGGER, **kwargs)

        # Registered after super().__init__, which puts the base class's own
        # async_shutdown on the entry first. Unload callbacks drain LIFO, so
        # ours run before it: stop the timers, then shut the coordinator down.
        entry.async_on_unload(self._async_cancel_timers)
        for anchor in (self._update_time, self._update_time_pm):
            if anchor is None:
                continue
            entry.async_on_unload(
                async_track_time_change(
                    hass,
                    self._handle_anchor,
                    hour=anchor.hour,
                    minute=anchor.minute,
                    second=anchor.second,
                )
            )

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    @callback
    def _async_cancel_timers(self) -> None:
        """Cancel the render timer and stop it from re-arming."""
        self._tearing_down = True
        if self._render_unsub is not None:
            self._render_unsub()
            self._render_unsub = None

    # ------------------------------------------------------------------
    # Render clock
    # ------------------------------------------------------------------

    @callback
    def _async_schedule_render(self, data: dict[str, Any] | None = None) -> None:
        """Arm a one-shot timer on the next instant an entity value can change.

        ``data`` lets a refresh arm the timer from the payload it just fetched,
        before the base class has assigned self.data.
        """
        if self._tearing_down or self.hass.is_stopping:
            return
        if self._render_unsub is not None:
            self._render_unsub()
            self._render_unsub = None
        target = self._next_render_instant(
            dt_util.now(), self.data if data is None else data
        )
        self._render_unsub = async_track_point_in_time(
            self.hass, self._handle_render, target
        )

    @callback
    def _handle_render(self, now: datetime) -> None:
        """Re-render entity state from the cached price curve."""
        self._render_unsub = None
        if self.hass.is_stopping:
            # The base class guards the fetch against shutdown but not the
            # listener notification, and cancel_on_shutdown does not reach a
            # point-in-time job, so the chain has to stop itself.
            return
        # Re-arm before rendering: async_track_point_in_time fires once, so a
        # failure while writing state must not break the chain for good.
        self._async_schedule_render()
        self.async_update_listeners()

    def _next_render_instant(
        self, now: datetime, data: dict[str, Any] | None
    ) -> datetime:
        """Return the next instant at which some entity value can change.

        Candidates come from the cached data itself - every slot start, plus the
        end of the covered range - taken as the union over all tariff types,
        because their grids can differ (say 15 minutes for electricity and 60
        for grid). Slot length is therefore never assumed.

        The result is capped at the next local midnight, which is when the
        today/tomorrow entities flip meaning. That cap is not derived from the
        data on purpose: when tomorrow's prices are missing there is no slot
        starting at midnight, and that is exactly when the today entities would
        otherwise keep showing yesterday's numbers.
        """
        local_now = dt_util.as_local(now)
        target = dt_util.start_of_local_day(local_now + timedelta(days=1))

        for tariff_data in (data or {}).values():
            if not isinstance(tariff_data, dict):
                continue
            prices = tariff_data.get("prices")
            if not isinstance(prices, list):
                continue
            for slot in prices:
                if not isinstance(slot, dict):
                    continue
                bounds = slot_bounds(slot)
                if bounds is None:
                    continue
                start, end = bounds
                # Slot starts only. Ends are stored as next_start - 1s and
                # find_current_slot compares inclusively, so firing on an end
                # would still match the old slot. The end of coverage is taken
                # as end + 1s so the last price goes stale rather than sticking.
                for candidate in (start, end + timedelta(seconds=1)):
                    # Strictly in the future: with CONF_QUERY_YEAR pointing at a
                    # past year every cached boundary is behind us, and a target
                    # in the past would fire immediately, in a tight loop.
                    if now < candidate < target:
                        target = candidate

        return target

    # ------------------------------------------------------------------
    # Fetch clock
    # ------------------------------------------------------------------

    async def _handle_anchor(self, now: datetime) -> None:
        """Fetch at one of the configured daily times."""
        # Every anchor starts a fresh ladder. Without this the failure counter
        # would only ever reset on a success, so once the backoff had given up
        # it stayed given up: from the next day on there was one attempt per
        # day and no retries at all.
        self._fail_count = 0
        self._hunt_count = 0
        # async_refresh rather than async_request_refresh: the latter goes
        # through a debouncer, which would swallow an anchor that lands close
        # to a retry.
        await self.async_refresh()

    def reference_now(self) -> datetime:
        """Return now, with the CONF_QUERY_YEAR test override applied."""
        now = dt_util.now()
        if self._query_year is None:
            return now
        try:
            return now.replace(year=self._query_year)
        except ValueError:
            # 29 February in a target year that has no such day.
            return now.replace(year=self._query_year, day=28)

    def covers_now(self, data: dict[str, Any] | None) -> bool:
        """Return True when every tariff type has a slot for the current instant.

        This is the condition behind "current price is unknown". It is checked
        instead of full coverage of today, so an API that only publishes from
        the current hour onwards does not look permanently incomplete.
        """
        if not data:
            return False
        reference = self.reference_now()
        for tariff_type in self._tariff_types:
            tariff_data = data.get(tariff_type)
            if not isinstance(tariff_data, dict):
                return False
            prices = tariff_data.get("prices")
            if not isinstance(prices, list):
                return False
            if find_current_slot(prices, reference) is None:
                return False
        return True

    def tomorrow_complete(self, data: dict[str, Any] | None) -> bool:
        """Return True when every tariff type covers tomorrow's local day.

        Coverage is measured in seconds rather than slot count, so it holds for
        any slot length and for the 23- and 25-hour days around a DST change. A
        partial publication - the API returning only the first few hours of
        tomorrow - correctly counts as incomplete.
        """
        if not data:
            return False
        local_ref = dt_util.as_local(self.reference_now())
        # Converted to UTC before any arithmetic: subtracting two aware
        # datetimes that share one tzinfo object ignores the offset, so on the
        # DST days a local-to-local subtraction reports 24 hours for days that
        # really run 23 or 25.
        day_start = dt_util.as_utc(dt_util.start_of_local_day(local_ref + timedelta(days=1)))
        day_end = dt_util.as_utc(dt_util.start_of_local_day(local_ref + timedelta(days=2)))
        span = (day_end - day_start).total_seconds()
        if span <= 0:
            return False

        for tariff_type in self._tariff_types:
            tariff_data = data.get(tariff_type)
            covered = 0.0
            if isinstance(tariff_data, dict):
                prices = tariff_data.get("prices")
                if isinstance(prices, list):
                    for slot in prices:
                        if not isinstance(slot, dict):
                            continue
                        bounds = slot_bounds(slot)
                        if bounds is None:
                            continue
                        start, end = bounds
                        overlap_start = max(dt_util.as_utc(start), day_start)
                        overlap_end = min(
                            dt_util.as_utc(end) + timedelta(seconds=1), day_end
                        )
                        if overlap_end > overlap_start:
                            covered += (overlap_end - overlap_start).total_seconds()
            if covered < span * TOMORROW_COVERAGE_RATIO:
                return False
        return True

    def _backoff_interval(self) -> timedelta | None:
        """Return the wait before retrying a failed fetch."""
        if self._fail_count > BACKOFF_MAX_TRIES:
            # Give up for now and wait for the next daily anchor.
            return None
        index = min(self._fail_count, len(BACKOFF_MINUTES)) - 1
        return timedelta(minutes=BACKOFF_MINUTES[index])

    def _hunt_interval(self, data: dict[str, Any]) -> timedelta | None:
        """Return the wait before asking for missing prices again.

        Two deficits, in order of severity. No slot for the current instant
        means the integration has no price to show at all, so that is chased on
        its own faster ladder with no time-of-day gate. A missing next day is
        only chased after the afternoon anchor, because day-ahead prices
        genuinely do not exist earlier. Both ladders are finite, so neither
        deficit can turn into an unbounded request stream.
        """
        local_now = dt_util.as_local(dt_util.now())
        if self._hunt_day != local_now.date():
            self._hunt_day = local_now.date()
            self._hunt_count = 0
            self._hunt_kind = None

        if self._query_year is not None:
            # Debug mode: "tomorrow" lives in the rewritten year and is
            # essentially never complete, so the anchors alone must do.
            return None

        if not self.covers_now(data):
            kind, ladder, gated = "today", TODAY_HUNT_MINUTES, False
        elif not self.tomorrow_complete(data):
            kind, ladder, gated = "tomorrow", HUNT_MINUTES, True
        else:
            self._hunt_count = 0
            self._hunt_kind = None
            return None

        if gated:
            if self._update_time_pm is None:
                return None
            if local_now.time() < self._update_time_pm:
                return None
            if local_now.hour >= HUNT_STOP_HOUR:
                return None

        if self._hunt_kind != kind:
            # Switching deficit starts that ladder from the top.
            self._hunt_kind = kind
            self._hunt_count = 0
        if self._hunt_count >= len(ladder):
            return None

        interval = timedelta(minutes=ladder[self._hunt_count])
        self._hunt_count += 1
        return interval

    def _failure_interval(self, err: Exception) -> timedelta | None:
        """Return the wait after a failed fetch, reading the HTTP status.

        api.py calls raise_for_status, so a rejected token or an unknown tariff
        name arrives as a ClientResponseError just like a network blip would.
        Laddering those is pointless: they do not get better by asking again.
        """
        status = getattr(err, "status", None)
        if isinstance(status, int):
            if status == 429:
                retry_after = _parse_retry_after(getattr(err, "headers", None))
                if retry_after is not None:
                    return retry_after
            elif 400 <= status < 500 and status != 408:
                _LOGGER.warning(
                    "The API rejected the request with HTTP %s. Check the "
                    "metering code, token or tariff name; retries are "
                    "suspended until the next scheduled fetch",
                    status,
                )
                return None
        return self._backoff_interval()

    def _note_failure(self, err: Exception) -> None:
        """Record a failed fetch and schedule the retry.

        The interval is set before the UpdateFailed propagates, because
        _async_refresh reads it in its finally block to schedule the retry.
        Any pending timer has already been cancelled at the top of that same
        method, so clearing the interval cannot leave one behind.
        """
        self._fail_count += 1
        self.update_interval = self._failure_interval(err)

    async def _async_update_data(self) -> dict[str, Any]:
        local_ref = dt_util.as_local(self.reference_now())
        start = dt_util.start_of_local_day(local_ref)
        # The query end is the exclusive start of the day after tomorrow. The
        # slot-fill end stays one second earlier: it is the fallback end for a
        # last slot the API did not terminate, and widening it would stretch
        # that slot by a second.
        query_end = dt_util.start_of_local_day(
            local_ref + timedelta(days=WINDOW_DAYS_FORWARD)
        )
        slot_fill_end = query_end - timedelta(seconds=1)

        try:
            data = await self._async_fetch_all(start, query_end, slot_fill_end)
        except (ClientError, TimeoutError, ValueError) as err:
            # TimeoutError matters: api.py wraps every request in a timeout, and
            # DataUpdateCoordinator handles a bare TimeoutError itself. That
            # would skip _note_failure and leave no retry scheduled at all.
            self._note_failure(err)
            raise UpdateFailed(f"Failed to fetch tariffs: {err}") from err
        except UpdateFailed as err:
            self._note_failure(err)
            raise

        self._fail_count = 0
        self.update_interval = self._hunt_interval(data)
        # The new payload can carry a different grid, so re-arm the render
        # timer from it. self.data is only assigned by the base class after
        # this returns, hence passing the payload explicitly.
        self._async_schedule_render(data)
        return data

    async def _async_fetch_all(
        self,
        start: datetime,
        query_end: datetime,
        slot_fill_end: datetime,
    ) -> dict[str, Any]:
        """Fetch every configured tariff type for the request window."""
        data: dict[str, Any] = {}

        for tariff_type in self._tariff_types:
            response = await self._client.fetch_tariffs(
                tariff_type=tariff_type,
                start=start,
                end=query_end,
                metering_code=self._metering_code,
                tariff_name=self._tariff_name,
            )

            # The ESIT API normally returns the price slots directly without a
            # wrapping ``status`` field. Only treat the response as an error when
            # an explicit, non-ok status is present.
            status = response.get("status")
            if isinstance(status, str) and status.lower() not in ("ok", "success"):
                raise UpdateFailed(
                    response.get("message")
                    or response.get("error")
                    or f"API error: {status}"
                )

            normalized = _normalize_tariff_response(response, window_end=slot_fill_end)
            if not normalized.get("prices"):
                _LOGGER.warning(
                    "No price slots returned for tariff_type=%s (response keys: %s)",
                    tariff_type,
                    list(response.keys()),
                )
            data[tariff_type] = normalized

        return data


def _normalize_tariff_response(
    response: dict[str, Any],
    *,
    window_end: datetime,
) -> dict[str, Any]:
    slots = None
    for key in ("prices", "data", "slots"):
        value = response.get(key)
        if isinstance(value, list):
            slots = value
            break
    if slots is None:
        return response

    normalized_slots = _normalize_slots(slots, window_end)
    normalized_response = dict(response)
    normalized_response["prices"] = normalized_slots
    return normalized_response


def _normalize_slots(slots: list[Any], window_end: datetime) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    starts: list[datetime | None] = []

    for slot in slots:
        if isinstance(slot, dict):
            slot_data = dict(slot)
        else:
            slot_data = {"value": slot}
        start = _coerce_datetime(
            _first_value(
                slot_data,
                "start_timestamp",
                "start",
                "start_time",
                "from",
                "timestamp",
                "time",
            )
        )
        starts.append(start)
        prepared.append(slot_data)

    for index, slot_data in enumerate(prepared):
        start = starts[index]
        end = _coerce_datetime(
            _first_value(
                slot_data,
                "end_timestamp",
                "end",
                "end_time",
                "to",
                "valid_until",
                "finish",
            )
        )
        if end is None and start is not None:
            next_start = None
            for future_start in starts[index + 1 :]:
                if future_start is not None:
                    next_start = future_start
                    break
            if next_start is not None:
                end = next_start - timedelta(seconds=1)
            else:
                end = window_end

        if start is not None:
            slot_data["start_timestamp"] = dt_util.as_local(start).isoformat()
        if end is not None:
            slot_data["end_timestamp"] = dt_util.as_local(end).isoformat()

    return prepared


def _first_value(slot: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = slot.get(key)
        if value is not None:
            return value
    return None


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 1_000_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=dt_util.UTC)
    if isinstance(value, str):
        return dt_util.parse_datetime(value)
    return None


def _coerce_time(value: Any) -> time | None:
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        return dt_util.parse_time(value)
    return None


def _parse_retry_after(headers: Any) -> timedelta | None:
    """Read a Retry-After header given as a number of seconds.

    The HTTP-date form is ignored on purpose: it is rare here and the escalating
    ladder is a safe fallback. The value is clamped so a hostile or broken
    header cannot park the integration for days.
    """
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        seconds = int(str(raw).strip())
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return timedelta(seconds=min(seconds, MAX_RETRY_AFTER_SECONDS))


def _coerce_year(value: Any) -> int | None:
    """Read the optional CONF_QUERY_YEAR test override."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None
