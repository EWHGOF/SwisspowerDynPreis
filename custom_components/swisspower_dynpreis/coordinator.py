"""Data coordinator for Swisspower DynPreis."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .api import SwisspowerDynPreisApiClient
from .const import (
    CONF_METERING_CODE,
    CONF_METHOD,
    CONF_API_URL,
    CONF_TARIFF_NAME,
    CONF_TARIFF_TYPES,
    CONF_TOKEN,
    CONF_UPDATE_INTERVAL,
    CONF_QUERY_YEAR,
    API_BASE,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class SwisspowerDynPreisCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for Swisspower DynPreis."""

    def __init__(self, hass: HomeAssistant, entry_data: dict[str, Any], options: dict[str, Any]) -> None:
        self._method = entry_data[CONF_METHOD]
        self._api_url = entry_data.get(CONF_API_URL, API_BASE)
        self._metering_code = entry_data.get(CONF_METERING_CODE)
        self._tariff_name = entry_data.get(CONF_TARIFF_NAME)
        self._tariff_types = entry_data[CONF_TARIFF_TYPES]
        self._token = entry_data.get(CONF_TOKEN)
        self._update_minutes = options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        self._query_year = options.get(CONF_QUERY_YEAR)

        session = async_get_clientsession(hass)
        self._client = SwisspowerDynPreisApiClient(
            session,
            self._method,
            self._token,
            api_base=self._api_url,
        )

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=self._update_minutes),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        now = dt_util.now()
        query_year = self._query_year
        if isinstance(query_year, str):
            query_year = query_year.strip() or None
            if query_year is not None:
                query_year = int(query_year)
        if isinstance(query_year, int):
            try:
                now = now.replace(year=query_year)
            except ValueError:
                now = now.replace(year=query_year, day=28)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=2) - timedelta(seconds=1)

        data: dict[str, Any] = {
            "_effective_now": now,
            "_window": {"start": start, "end": end},
        }

        for tariff_type in self._tariff_types:
            try:
                response = await self._client.fetch_tariffs(
                    tariff_type=tariff_type,
                    start=start,
                    end=end,
                    metering_code=self._metering_code,
                    tariff_name=self._tariff_name,
                )
            except (ClientError, ValueError) as err:
                raise UpdateFailed(f"Failed to fetch tariffs: {err}") from err

            if response.get("status") != "ok":
                raise UpdateFailed(response.get("message", "Unknown API error"))

            data[tariff_type] = _normalize_tariff_response(response, window_end=end)

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
