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
    TOMORROW_COVERAGE_RATIO,
    WINDOW_DAYS_FORWARD,
)
from .pricing import slot_bounds

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
        # config_entry is deliberately not passed to super(): that keyword does
        # not exist on homeassistant 2024.3, and on current versions the base
        # class fills self.config_entry from the current_entry ContextVar and
        # explicitly does not enforce passing it for custom integrations.
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,
        )

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
        if self._tearing_down:
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
        day_start = dt_util.start_of_local_day(local_ref + timedelta(days=1))
        day_end = dt_util.start_of_local_day(local_ref + timedelta(days=2))
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
                        overlap_start = max(start, day_start)
                        overlap_end = min(end + timedelta(seconds=1), day_end)
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
        """Return the wait before looking for tomorrow's prices again."""
        local_now = dt_util.as_local(dt_util.now())
        if self._hunt_day != local_now.date():
            self._hunt_day = local_now.date()
            self._hunt_count = 0

        if self._query_year is not None:
            # Debug mode: "tomorrow" lives in the rewritten year and is
            # essentially never complete, so the anchors alone must do.
            return None
        if self.tomorrow_complete(data):
            self._hunt_count = 0
            return None
        if self._update_time_pm is None:
            return None
        if local_now.time() < self._update_time_pm:
            # Tomorrow's prices genuinely do not exist yet in the morning.
            return None
        if local_now.hour >= HUNT_STOP_HOUR:
            return None
        if self._hunt_count >= len(HUNT_MINUTES):
            return None

        interval = timedelta(minutes=HUNT_MINUTES[self._hunt_count])
        self._hunt_count += 1
        return interval

    def _note_failure(self) -> None:
        """Record a failed fetch and schedule the retry.

        The interval is set before the UpdateFailed propagates, because
        _async_refresh reads it in its finally block to schedule the retry.
        Any pending timer has already been cancelled at the top of that same
        method, so clearing the interval cannot leave one behind.
        """
        self._fail_count += 1
        self.update_interval = self._backoff_interval()

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
            self._note_failure()
            raise UpdateFailed(f"Failed to fetch tariffs: {err}") from err
        except UpdateFailed:
            self._note_failure()
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
