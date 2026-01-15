"""API client for Swisspower ESIT."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import async_timeout
from aiohttp import ClientSession
from homeassistant.util import dt as dt_util

from .const import API_BASE, METHOD_METERING_CODE, TIMEOUT_SECONDS


class SwisspowerDynPreisApiClient:
    """API client for Swisspower ESIT."""

    def __init__(
        self,
        session: ClientSession,
        method: str,
        token: str | None,
        api_base: str = API_BASE,
    ) -> None:
        self._session = session
        self._method = method
        self._token = token
        self._api_base = api_base

    async def fetch_tariffs(
        self,
        *,
        tariff_type: str,
        start: datetime,
        end: datetime,
        metering_code: str | None = None,
        tariff_name: str | None = None,
    ) -> dict[str, Any]:
        """Fetch tariffs for a given tariff type."""
        params: dict[str, Any] = {
            "tariff_type": tariff_type,
            "start_timestamp": dt_util.as_local(start).isoformat(),
            "end_timestamp": dt_util.as_local(end).isoformat(),
        }

        headers: dict[str, str] = {}
        path = "/tariff_name"

        if self._method == METHOD_METERING_CODE:
            path = "/metering_code"
            params["metering_code"] = metering_code
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
        else:
            params["tariff_name"] = tariff_name

        url = f"{self._api_base}{path}"

        async with async_timeout.timeout(TIMEOUT_SECONDS):
            async with self._session.get(url, params=params, headers=headers) as response:
                response.raise_for_status()
                return await response.json()
