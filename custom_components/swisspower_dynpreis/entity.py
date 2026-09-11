"""Shared entity plumbing for the Swisspower DynPreis platforms."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SwisspowerDynPreisCoordinator


def device_info_for(entry_id: str, name: str) -> DeviceInfo:
    """Return the device every entity of one config entry belongs to.

    A free function rather than a method on the base class below, because the
    refresh button deliberately does not inherit from it - it has no tariff
    type and no coordinator-derived state - but must still land on the same
    device instead of creating a second one.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name=name,
        manufacturer="Swisspower",
    )


class SwisspowerDynPreisEntity(CoordinatorEntity[SwisspowerDynPreisCoordinator]):
    """Base for every entity of one tariff type."""

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
        return device_info_for(self._entry_id, self._name)

    @property
    def available(self) -> bool:
        """Return whether this tariff type has data.

        The base class ties availability to the single global
        last_update_success, so one failing tariff type used to make every
        entity of every type unavailable. The coordinator now carries a type's
        previous payload forward, and a type is available as long as it has
        one - a type that never returned anything stays unavailable.
        """
        if not self.coordinator.last_update_success:
            return False
        data = self.coordinator.data
        return isinstance(data, dict) and self._tariff_type in data

    @property
    def price_slots(self) -> list[dict[str, Any]]:
        """Return the cached slots for this tariff type, or an empty list.

        Guards every shape the API and the first refresh can produce: no data
        yet, a tariff type the response omitted, or a payload whose prices key
        is not a list.
        """
        data = self.coordinator.data
        if not isinstance(data, dict):
            return []
        tariff_data = data.get(self._tariff_type)
        if not isinstance(tariff_data, dict):
            return []
        prices = tariff_data.get("prices")
        return prices if isinstance(prices, list) else []
