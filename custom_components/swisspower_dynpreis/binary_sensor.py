"""Binary sensors for Swisspower DynPreis.

These used to be added through the sensor platform, which put them in the
sensor domain with a state of "on" or "off". They belong here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_TARIFF_TYPES, DEFAULT_NAME, DOMAIN
from .coordinator import SwisspowerDynPreisCoordinator
from .entity import SwisspowerDynPreisEntity
from .stats import in_window, percentile_binary


@dataclass(frozen=True)
class BinaryDescription:
    """One derived on/off sensor."""

    key: str
    name: str
    enabled_default: bool
    value_fn: Callable[[list[dict[str, Any]], datetime, str, str | None], bool | None]


# key, name, share of the day. Spelled out so every entity key is greppable.
_PERCENTILE_SENSORS: tuple[tuple[str, str, float, bool], ...] = (
    ("cheapest_25_today", "Cheapest 25% hours today", 0.25, False),
    ("cheapest_10_today", "Cheapest 10% hours today", 0.10, False),
    ("cheapest_50_today", "Cheapest 50% hours today", 0.50, False),
    ("expensive_25_today", "Most expensive 25% hours today", 0.25, True),
    ("expensive_10_today", "Most expensive 10% hours today", 0.10, True),
)

# key, name, window length in hours, cheapest ("min") or dearest ("max").
_WINDOW_SENSORS: tuple[tuple[str, str, int, str], ...] = (
    ("in_cheapest_2h_today", "In cheapest 2h window today", 2, "min"),
    ("in_cheapest_4h_today", "In cheapest 4h window today", 4, "min"),
    ("in_expensive_2h_today", "In most expensive 2h window today", 2, "max"),
    ("in_expensive_4h_today", "In most expensive 4h window today", 4, "max"),
)


def _percentile_description(
    key: str, name: str, percentile: float, highest: bool
) -> BinaryDescription:
    return BinaryDescription(
        key=key,
        name=name,
        enabled_default=True,
        value_fn=lambda slots, now, tariff, component: percentile_binary(
            slots, now, percentile, tariff, component, highest
        ),
    )


def _window_description(
    key: str, name: str, hours: int, extreme: str
) -> BinaryDescription:
    return BinaryDescription(
        key=key,
        name=name,
        enabled_default=True,
        value_fn=lambda slots, now, tariff, component: in_window(
            slots, now, 0, hours, tariff, component, extreme
        ),
    )


BINARY_DESCRIPTIONS: tuple[BinaryDescription, ...] = tuple(
    _percentile_description(*row) for row in _PERCENTILE_SENSORS
) + tuple(_window_description(*row) for row in _WINDOW_SENSORS)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Swisspower DynPreis binary sensors."""
    coordinator: SwisspowerDynPreisCoordinator = hass.data[DOMAIN][entry.entry_id]
    name = entry.title or DEFAULT_NAME

    async_add_entities(
        SwisspowerDynPreisBinarySensor(
            coordinator=coordinator,
            entry_id=entry.entry_id,
            name=name,
            tariff_type=tariff_type,
            description=description,
        )
        for tariff_type in entry.data[CONF_TARIFF_TYPES]
        for description in BINARY_DESCRIPTIONS
    )


class SwisspowerDynPreisBinarySensor(SwisspowerDynPreisEntity, BinarySensorEntity):
    """An on/off statement about the current price."""

    def __init__(
        self,
        *,
        coordinator: SwisspowerDynPreisCoordinator,
        entry_id: str,
        name: str,
        tariff_type: str,
        description: BinaryDescription,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            entry_id=entry_id,
            name=name,
            tariff_type=tariff_type,
        )
        # Deliberately NOT called ``entity_description``: Home Assistant reads
        # its own fields off that attribute whenever it exists, and
        # BinaryDescription is not a BinarySensorEntityDescription.
        self._description = description

    @property
    def name(self) -> str:
        return f"{self._name} {self._tariff_type} {self._description.name}"

    @property
    def unique_id(self) -> str:
        return f"{self._entry_id}_{self._tariff_type}_{self._description.key}"

    @property
    def entity_registry_enabled_default(self) -> bool:
        return self._description.enabled_default

    @property
    def is_on(self) -> bool | None:
        # None means "no data for this instant" and stays unknown. Reporting it
        # as a confident "off" is a dangerous input for automations, and state
        # is re-rendered at every price boundary.
        return self._description.value_fn(
            self.price_slots, dt_util.now(), self._tariff_type, None
        )
