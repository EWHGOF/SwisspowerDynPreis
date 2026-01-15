"""API client for Swisspower ESIT."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

import async_timeout
from aiohttp import ClientSession
from homeassistant.util import dt as dt_util
from yarl import URL

from .const import API_BASE, METHOD_METERING_CODE, TIMEOUT_SECONDS

LOGGER = logging.getLogger(__name__)


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
        start_timestamp = dt_util.as_local(start).isoformat()
        end_timestamp = dt_util.as_local(end).isoformat()
        params: dict[str, Any] = {
            "tariff_type": tariff_type,
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
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

        query = urlencode(params, quote_via=quote, safe="")
        url = URL(f"{self._api_base}{path}?{query}", encoded=True)

        async with async_timeout.timeout(TIMEOUT_SECONDS):
            async with self._session.get(url, headers=headers) as response:
                response_text = await response.text()
                LOGGER.info(
                    "SwisspowerDynPreis API request: method=GET url=%s headers=%s",
                    url,
                    headers,
                )
                LOGGER.info(
                    "SwisspowerDynPreis API response: status=%s headers=%s body=%s",
                    response.status,
                    dict(response.headers),
                    response_text,
                )
                response.raise_for_status()
                try:
                    response_data: Any = json.loads(response_text)
                except json.JSONDecodeError:
                    response_data = {"raw": response_text}
                if not isinstance(response_data, dict):
                    response_data = {"data": response_data}
                return response_data
