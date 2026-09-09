"""Sensors for Swisspower DynPreis."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_TARIFF_TYPES, DEFAULT_NAME, DOMAIN
from .coordinator import SwisspowerDynPreisCoordinator
from .entity import SwisspowerDynPreisEntity
from .pricing import extract_slot_value, find_current_slot
from .stats import (
    average_for_day,
    day_stats,
    next_change,
    window_attrs,
    window_value,
)


@dataclass(frozen=True)
class SensorDescription:
    """One derived numeric or timestamp sensor."""

    key: str
    name: str
    enabled_default: bool
    unit: str | None
    value_fn: Callable[[list[dict[str, Any]], datetime, str, str | None], Any]
    extra_fn: (
        Callable[[list[dict[str, Any]], datetime, str, str | None], dict[str, Any]]
        | None
    ) = None
    device_class: SensorDeviceClass | None = None


_DERIVED_DESCRIPTIONS: tuple[SensorDescription, ...] = (
    SensorDescription(
        key="next_change",
        name="Next change",
        enabled_default=True,
        unit=None,
        value_fn=lambda slots, now, tariff, component: next_change(
            slots, now, tariff, component
        ),
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    SensorDescription(
        key="avg_today",
        name="Average price today",
        enabled_default=True,
        unit="CHF/kWh",
        value_fn=lambda slots, now, tariff, component: average_for_day(
            slots, now, 0, tariff, component
        ),
        extra_fn=lambda slots, now, tariff, component: day_stats(
            slots, now, 0, tariff, component
        ),
    ),
    SensorDescription(
        key="avg_tomorrow",
        name="Average price tomorrow",
        enabled_default=True,
        unit="CHF/kWh",
        value_fn=lambda slots, now, tariff, component: average_for_day(
            slots, now, 1, tariff, component
        ),
        extra_fn=lambda slots, now, tariff, component: day_stats(
            slots, now, 1, tariff, component
        ),
    ),
)

# key, name, day offset, window length in hours, cheapest ("min") or dearest.
# Spelled out rather than generated so every entity key is greppable.
_WINDOW_SENSORS: tuple[tuple[str, str, int, int, str], ...] = (
    ("lowest_2h_today", "Lowest 2h window today", 0, 2, "min"),
    ("lowest_2h_tomorrow", "Lowest 2h window tomorrow", 1, 2, "min"),
    ("lowest_4h_today", "Lowest 4h window today", 0, 4, "min"),
    ("lowest_4h_tomorrow", "Lowest 4h window tomorrow", 1, 4, "min"),
    ("highest_2h_today", "Highest 2h window today", 0, 2, "max"),
    ("highest_2h_tomorrow", "Highest 2h window tomorrow", 1, 2, "max"),
    ("highest_4h_today", "Highest 4h window today", 0, 4, "max"),
    ("highest_4h_tomorrow", "Highest 4h window tomorrow", 1, 4, "max"),
)


def _window_description(
    key: str, name: str, offset_days: int, hours: int, extreme: str
) -> SensorDescription:
    """Build one of the lowest/highest window sensors."""
    return SensorDescription(
        key=key,
        name=name,
        enabled_default=True,
        unit="CHF/kWh",
        value_fn=lambda slots, now, tariff, component: window_value(
            slots, now, offset_days, hours, tariff, component, extreme
        ),
        extra_fn=lambda slots, now, tariff, component: window_attrs(
            slots, now, offset_days, hours, tariff, component, extreme
        ),
    )


SENSOR_DESCRIPTIONS: tuple[SensorDescription, ...] = _DERIVED_DESCRIPTIONS + tuple(
    _window_description(*row) for row in _WINDOW_SENSORS
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Swisspower DynPreis sensors."""
    coordinator: SwisspowerDynPreisCoordinator = hass.data[DOMAIN][entry.entry_id]
    name = entry.title or DEFAULT_NAME

    tariff_types = entry.data[CONF_TARIFF_TYPES]
    entities: list[SwisspowerDynPreisEntity] = []
    component_map = _collect_components(coordinator.data, tariff_types)
    for tariff_type in tariff_types:
        entities.append(
            SwisspowerDynPreisCurrentPriceSensor(
                coordinator=coordinator,
                entry_id=entry.entry_id,
                name=name,
                tariff_type=tariff_type,
            )
        )
        for component in sorted(component_map.get(tariff_type, set())):
            entities.append(
                SwisspowerDynPreisCurrentPriceSensor(
                    coordinator=coordinator,
                    entry_id=entry.entry_id,
                    name=name,
                    tariff_type=tariff_type,
                    component=component,
                )
            )
        entities.extend(
            SwisspowerDynPreisStatSensor(
                coordinator=coordinator,
                entry_id=entry.entry_id,
                name=name,
                tariff_type=tariff_type,
                description=description,
            )
            for description in SENSOR_DESCRIPTIONS
        )

    async_add_entities(entities)


class SwisspowerDynPreisCurrentPriceSensor(SwisspowerDynPreisEntity, SensorEntity):
    """The price that applies right now."""

    # The whole price curve is an attribute, and state is now written at every
    # price boundary - up to 96 times a day with quarter-hourly tariffs. Left
    # in, the recorder would store the full curve on every one of those writes.
    _unrecorded_attributes = frozenset({"prices"})

    def __init__(
        self,
        *,
        coordinator: SwisspowerDynPreisCoordinator,
        entry_id: str,
        name: str,
        tariff_type: str,
        component: str | None = None,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            entry_id=entry_id,
            name=name,
            tariff_type=tariff_type,
        )
        self._component = component

    @property
    def name(self) -> str:
        if self._component:
            return f"{self._name} {self._tariff_type} {self._component} Current price"
        return f"{self._name} {self._tariff_type} Current price"

    @property
    def unique_id(self) -> str:
        if self._component:
            return (
                f"{self._entry_id}_{self._tariff_type}_{self._component}_current_price"
            )
        return f"{self._entry_id}_{self._tariff_type}_current_price"

    @property
    def native_unit_of_measurement(self) -> str | None:
        return "CHF/kWh"

    @property
    def native_value(self) -> float | None:
        slot = find_current_slot(self.price_slots, dt_util.now())
        if not slot:
            return None
        return extract_slot_value(slot, self._tariff_type, self._component)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slots = self.price_slots
        slot = find_current_slot(slots, dt_util.now())
        current_start = None
        current_end = None
        current_value = None
        if slot:
            current_start = slot.get("start_timestamp")
            current_end = slot.get("end_timestamp")
            current_value = extract_slot_value(slot, self._tariff_type, self._component)
        last_success = self.coordinator.last_success(self._tariff_type)
        return {
            "tariff_type": self._tariff_type,
            "component": self._component,
            "prices": slots,
            "current_start_timestamp": current_start,
            "current_end_timestamp": current_end,
            "current_value": current_value,
            # So an outage that is being masked by cached prices is still
            # visible, in the state machine and to automations.
            "last_successful_fetch": (
                last_success.isoformat() if last_success else None
            ),
            "from_cache": self.coordinator.tariff_is_stale(self._tariff_type),
        }


class SwisspowerDynPreisStatSensor(SwisspowerDynPreisEntity, SensorEntity):
    """A statistic derived from the cached price curve."""

    def __init__(
        self,
        *,
        coordinator: SwisspowerDynPreisCoordinator,
        entry_id: str,
        name: str,
        tariff_type: str,
        description: SensorDescription,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            entry_id=entry_id,
            name=name,
            tariff_type=tariff_type,
        )
        # Deliberately NOT called ``entity_description``: Home Assistant reads
        # its own fields (has_entity_name, suggested_unit_of_measurement, ...)
        # off that attribute whenever it exists, and SensorDescription is not a
        # SensorEntityDescription. Naming it that way made every stat entity
        # fail to be added with an AttributeError.
        self._description = description

    @property
    def name(self) -> str:
        return f"{self._name} {self._tariff_type} {self._description.name}"

    @property
    def unique_id(self) -> str:
        return f"{self._entry_id}_{self._tariff_type}_{self._description.key}"

    @property
    def native_unit_of_measurement(self) -> str | None:
        return self._description.unit

    @property
    def device_class(self) -> SensorDeviceClass | None:
        return self._description.device_class

    @property
    def entity_registry_enabled_default(self) -> bool:
        return self._description.enabled_default

    @property
    def native_value(self) -> Any:
        return self._description.value_fn(
            self.price_slots, dt_util.now(), self._tariff_type, None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self._description.extra_fn:
            return {}
        return self._description.extra_fn(
            self.price_slots, dt_util.now(), self._tariff_type, None
        )


def _collect_components(
    data: dict[str, Any] | None,
    tariff_types: list[str],
) -> dict[str, set[str]]:
    """Find the price components the API returned per tariff type."""
    components: dict[str, set[str]] = {
        tariff_type: set() for tariff_type in tariff_types
    }
    if not isinstance(data, dict):
        return components
    for tariff_type in tariff_types:
        tariff_data = data.get(tariff_type)
        if not isinstance(tariff_data, dict):
            continue
        slots = tariff_data.get("prices")
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
