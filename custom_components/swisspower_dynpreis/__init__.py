"""Swisspower DynPreis integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .binary_sensor import BINARY_DESCRIPTIONS
from .const import CONF_TARIFF_TYPES, DOMAIN
from .coordinator import SwisspowerDynPreisCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Swisspower DynPreis from a config entry."""
    coordinator = SwisspowerDynPreisCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    _async_remove_legacy_binary_entities(hass, entry)

    # Reload on an options change so a new fetch time or test year takes effect
    # without restarting Home Assistant. Wrapping the remover in
    # entry.async_on_unload is what keeps the listener list from growing by one
    # on every reload.
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _async_remove_legacy_binary_entities(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Drop the on/off entities that used to live in the sensor domain.

    They were added through the sensor platform, so they exist in the registry
    as sensor.* with a state of "on" or "off". The binary sensor platform
    creates them again under binary_sensor.*, and without this the old rows
    would linger for ever as unavailable entities the integration no longer
    provides. Only the exact unique ids this integration knows are touched.
    """
    registry = er.async_get(hass)
    for tariff_type in entry.data[CONF_TARIFF_TYPES]:
        for description in BINARY_DESCRIPTIONS:
            unique_id = f"{entry.entry_id}_{tariff_type}_{description.key}"
            entity_id = registry.async_get_entity_id(
                Platform.SENSOR, DOMAIN, unique_id
            )
            if entity_id is None:
                continue
            _LOGGER.info(
                "Removing %s: this entity moved to the binary_sensor domain",
                entity_id,
            )
            registry.async_remove(entity_id)


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry after its options changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    The coordinator's timers are cancelled through entry.async_on_unload, which
    Home Assistant drains itself - including the base class's own
    async_shutdown, which the integration used to shadow with a sync method of
    the same name and then call without awaiting.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
