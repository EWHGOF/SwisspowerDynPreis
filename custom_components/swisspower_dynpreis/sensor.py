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
    entities: list[SwisspowerDynPreisSensor] = []
    component_map = _collect_components(coordinator.data, tariff_types)
    for tariff_type in tariff_types:
        entities.append(
            SwisspowerDynPreisSensor(
                coordinator=coordinator,
                entry_id=entry.entry_id,
                name=name,
                tariff_type=tariff_type,
                component=None,
            )
        )
        for component in sorted(component_map.get(tariff_type, set())):
            entities.append(
                SwisspowerDynPreisSensor(
                    coordinator=coordinator,
                    entry_id=entry.entry_id,
                    name=name,
                    tariff_type=tariff_type,
                    component=component,
                )
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
        component: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._name = name
        self._tariff_type = tariff_type
        self._component = component

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=self._name,
            manufacturer="Swisspower",
        )

    @property
    def name(self) -> str:
        if self._component:
            return f"{self._name} {self._tariff_type} {self._component}"
        return f"{self._name} {self._tariff_type}"

    @property
    def unique_id(self) -> str:
        if self._component:
            return f"{self._entry_id}_{self._tariff_type}_{self._component}"
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
        return _extract_slot_value(slot, self._tariff_type, self._component)

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
            current_value = _extract_slot_value(slot, self._tariff_type, self._component)
        return {
            "tariff_type": self._tariff_type,
            "component": self._component,
            "prices": data.get("prices", []),
            "current_start_timestamp": current_start,
            "current_end_timestamp": current_end,
            "current_value": current_value,
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


def _extract_slot_value(
    slot: dict[str, Any],
    tariff_type: str,
    component: str | None,
) -> float | None:
    if component is None and isinstance(slot.get("value"), (int, float)):
        return slot.get("value")
    prices = slot.get(tariff_type)
    if isinstance(prices, list):
        for price in prices:
            if not isinstance(price, dict):
                continue
            if component is not None and price.get("component") != component:
                continue
            if price.get("unit") == "CHF/kWh" and price.get("value") is not None:
                return price.get("value")
            if price.get("value") is not None:
                return price.get("value")
    if slot.get("unit") == "CHF/kWh":
        if component is None and slot.get("component") in (None, "work"):
            return slot.get("value")
        if component is not None and slot.get("component") == component:
            return slot.get("value")
    return None


def _collect_components(
    data: dict[str, Any],
    tariff_types: list[str],
) -> dict[str, set[str]]:
    components: dict[str, set[str]] = {tariff_type: set() for tariff_type in tariff_types}
    for tariff_type in tariff_types:
        tariff_data = data.get(tariff_type, {})
        slots = tariff_data.get("prices", [])
        if not isinstance(slots, list):
            continue
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            prices = slot.get(tariff_type)
            if isinstance(prices, list):
                for price in prices:
                    if not isinstance(price, dict):
                        continue
                    component = price.get("component")
                    if isinstance(component, str) and component:
                        components[tariff_type].add(component)
            component = slot.get("component")
            if isinstance(component, str) and component:
                components[tariff_type].add(component)
    return components
