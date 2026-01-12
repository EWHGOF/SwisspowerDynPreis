"""Sensor for Swisspower dynamic price integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .api import SwisspowerApiClient, SwisspowerApiConfig
from .const import (
    CONF_METERING_CODE,
    CONF_TARIFF_NAME,
    CONF_TARIFF_TYPE,
    CONF_TOKEN,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)

LOGGER = logging.getLogger(__name__)

@dataclass
class SwisspowerPriceState:
    """Prepared price state."""

    price: float | None
    unit: str | None
    start: datetime | None
    end: datetime | None
    components: list[dict[str, Any]]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Swisspower sensor based on a config entry."""
    session = async_get_clientsession(hass)
    config = SwisspowerApiConfig(
        token=entry.data[CONF_TOKEN],
        metering_code=entry.data.get(CONF_METERING_CODE),
        tariff_name=entry.data.get(CONF_TARIFF_NAME),
        tariff_type=entry.data[CONF_TARIFF_TYPE],
    )
    client = SwisspowerApiClient(session, config)

    coordinator = SwisspowerTariffCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    async_add_entities([SwisspowerPriceSensor(entry, coordinator)])


class SwisspowerTariffCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to fetch Swisspower tariffs."""

    def __init__(self, hass: HomeAssistant, client: SwisspowerApiClient) -> None:
        super().__init__(
            hass,
            logger=LOGGER,
            name="swisspower_tariffs",
            update_interval=timedelta(minutes=DEFAULT_UPDATE_INTERVAL),
        )
        self._client = client

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self._client.async_get_tariffs()
        except Exception as err:
            raise UpdateFailed(str(err)) from err

        if data.get("status") != "ok":
            raise UpdateFailed(data.get("message", "Unknown API error"))

        return data


class SwisspowerPriceSensor(CoordinatorEntity[SwisspowerTariffCoordinator], SensorEntity):
    """Sensor for current Swisspower price."""

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, coordinator: SwisspowerTariffCoordinator) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_current_price"
        self._attr_name = "Current price"

    @property
    def native_value(self) -> float | None:
        return self._price_state.price

    @property
    def native_unit_of_measurement(self) -> str | None:
        return self._price_state.unit

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self._price_state
        return {
            "slot_start": state.start.isoformat() if state.start else None,
            "slot_end": state.end.isoformat() if state.end else None,
            "components": state.components,
            "tariff_type": self._entry.data[CONF_TARIFF_TYPE],
        }

    @property
    def available(self) -> bool:
        return self._coordinator.last_update_success

    @property
    def _price_state(self) -> SwisspowerPriceState:
        data = self.coordinator.data
        if not data:
            return SwisspowerPriceState(None, None, None, None, [])

        now = dt_util.now()
        prices = data.get("prices", [])
        for slot in prices:
            start = dt_util.parse_datetime(slot.get("start_timestamp", ""))
            end = dt_util.parse_datetime(slot.get("end_timestamp", ""))
            if not start or not end:
                continue
            if start <= now < end:
                return _state_from_slot(slot, self._entry.data[CONF_TARIFF_TYPE])

        return SwisspowerPriceState(None, None, None, None, [])

def _state_from_slot(slot: dict[str, Any], tariff_type: str) -> SwisspowerPriceState:
    components = []
    values: list[float] = []
    units: set[str] = set()

    for price in slot.get(tariff_type, []) or []:
        value = price.get("value")
        unit = price.get("unit")
        component = price.get("component")
        components.append({"value": value, "unit": unit, "component": component})
        if isinstance(value, (int, float)) and unit:
            values.append(float(value))
            units.add(unit)

    unit = units.pop() if len(units) == 1 else None
    total = sum(values) if unit else None

    start = dt_util.parse_datetime(slot.get("start_timestamp", ""))
    end = dt_util.parse_datetime(slot.get("end_timestamp", ""))

    return SwisspowerPriceState(total, unit, start, end, components)
