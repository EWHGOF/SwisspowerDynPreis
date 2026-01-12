"""API client for Swisspower dynamic tariff prices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.util import dt as dt_util

from .const import API_BASE_URL


@dataclass
class SwisspowerApiConfig:
    """Configuration for the Swisspower API client."""

    token: str
    metering_code: str | None
    tariff_name: str | None
    tariff_type: str


class SwisspowerApiClient:
    """Client for Swisspower ESIT API."""

    def __init__(self, session: aiohttp.ClientSession, config: SwisspowerApiConfig) -> None:
        self._session = session
        self._config = config

    async def async_get_tariffs(self) -> dict[str, Any]:
        """Fetch tariffs for the configured meter or tariff name."""
        now = dt_util.now()
        start_timestamp = _to_rfc3339(now)
        end_timestamp = _to_rfc3339(now + timedelta(hours=24))

        if self._config.metering_code:
            endpoint = f"{API_BASE_URL}/metering_code"
            params = {
                "metering_code": self._config.metering_code,
                "tariff_type": self._config.tariff_type,
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
            }
        else:
            endpoint = f"{API_BASE_URL}/tariff_name"
            params = {
                "tariff_name": self._config.tariff_name,
                "tariff_type": self._config.tariff_type,
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
            }

        headers = {"Authorization": f"Bearer {self._config.token}"}

        async with self._session.get(endpoint, params=params, headers=headers) as response:
            response.raise_for_status()
            data: dict[str, Any] = await response.json()

        return data


def _to_rfc3339(value: datetime) -> str:
    return dt_util.as_local(value).isoformat()
