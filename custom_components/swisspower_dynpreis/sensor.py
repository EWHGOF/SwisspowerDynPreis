"""Sensors for Swisspower DynPreis."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_TARIFF_TYPES, DEFAULT_NAME, DOMAIN
from .coordinator import SwisspowerDynPreisCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Swisspower DynPreis sensors."""
    coordinator: SwisspowerDynPreisCoordinator = hass.data[DOMAIN][entry.entry_id]
    name = entry.title or DEFAULT_NAME

    tariff_types = entry.data[CONF_TARIFF_TYPES]
    entities = [
        SwisspowerDynPreisSensor(
            coordinator=coordinator,
            entry_id=entry.entry_id,
            name=name,
            tariff_type=tariff_type,
        )
        for tariff_type in tariff_types
    ]
    entities.extend(
        SwisspowerDynPreisScheduleSensor(
            coordinator=coordinator,
            entry_id=entry.entry_id,
            name=name,
            tariff_type=tariff_type,
        )
        for tariff_type in tariff_types
    )

    async_add_entities(entities)


class SwisspowerDynPreisSensor(CoordinatorEntity[SwisspowerDynPreisCoordinator], SensorEntity):
    """Representation of a Swisspower DynPreis tariff."""

    _attr_should_poll = False

    def __init__(
        self,
        *,
        coordinator: SwisspowerDynPreisCoordinator,
        entry_id: str,
        name: str,
        tariff_type: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._name = name
        self._tariff_type = tariff_type

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=self._name,
            manufacturer="Swisspower",
        )

    @property
    def name(self) -> str:
        return f"{self._name} {self._tariff_type}"

    @property
    def unique_id(self) -> str:
        return f"{self._entry_id}_{self._tariff_type}"

    @property
    def native_unit_of_measurement(self) -> str | None:
        return "CHF/kWh"

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data.get(self._tariff_type, {})
        slot = _find_slot(data.get("prices", []), dt_util.now())
        if not slot:
            return None
        return _extract_slot_value(slot, self._tariff_type)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data.get(self._tariff_type, {})
        slot = _find_slot(data.get("prices", []), dt_util.now())
        current_start = None
        current_end = None
        current_value = None
        if slot:
            current_start = slot.get("start_timestamp")
            current_end = slot.get("end_timestamp")
            current_value = _extract_slot_value(slot, self._tariff_type)
        return {
            "tariff_type": self._tariff_type,
            "prices": data.get("prices", []),
            "current_start_timestamp": current_start,
            "current_end_timestamp": current_end,
            "current_value": current_value,
        }


class SwisspowerDynPreisScheduleSensor(
    CoordinatorEntity[SwisspowerDynPreisCoordinator], SensorEntity
):
    """Representation of a Swisspower DynPreis schedule."""

    _attr_should_poll = False

    def __init__(
        self,
        *,
        coordinator: SwisspowerDynPreisCoordinator,
        entry_id: str,
        name: str,
        tariff_type: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._name = name
        self._tariff_type = tariff_type

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=self._name,
            manufacturer="Swisspower",
        )

    @property
    def name(self) -> str:
        return f"{self._name} {self._tariff_type} schedule"

    @property
    def unique_id(self) -> str:
        return f"{self._entry_id}_{self._tariff_type}_schedule"

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data.get(self._tariff_type, {})
        prices = data.get("prices")
        if isinstance(prices, list):
            return len(prices)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data.get(self._tariff_type, {})
        return {
            "tariff_type": self._tariff_type,
            "prices": data.get("prices", []),
        }


def _find_slot(slots: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    for slot in slots:
        start = dt_util.parse_datetime(slot.get("start_timestamp"))
        end = dt_util.parse_datetime(slot.get("end_timestamp"))
        if not start or not end:
            continue
        if start <= now <= end:
            return slot
    return None


def _extract_slot_value(slot: dict[str, Any], tariff_type: str) -> float | None:
    if isinstance(slot.get("value"), (int, float)):
        return slot.get("value")
    prices = slot.get(tariff_type)
    if isinstance(prices, list):
        for price in prices:
            if not isinstance(price, dict):
                continue
            if price.get("unit") == "CHF/kWh" and price.get("component") == "work":
                return price.get("value")
            if price.get("value") is not None:
                return price.get("value")
    if slot.get("unit") == "CHF/kWh" and slot.get("component") in (None, "work"):
        return slot.get("value")
    return None
