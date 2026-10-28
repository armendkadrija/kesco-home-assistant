"""KESCO Energy integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api import KescoApiClient, async_create_kesco_session
from .const import DOMAIN, CONF_AGENCY_ID, CONF_DEBITOR_ID
from .coordinator import KescoCoordinator

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = await async_create_kesco_session(hass)
    # Closes on unload and on setup failure (HA runs unload callbacks in both cases)
    entry.async_on_unload(session.close)
    client = KescoApiClient(
        username=entry.data["username"],
        password=entry.data["password"],
        agency_id=entry.data[CONF_AGENCY_ID],
        debitor_id=entry.data[CONF_DEBITOR_ID],
        session=session,
    )
    coordinator = KescoCoordinator(hass, client)
    # Store coordinator reference on the entry so sensors can access it
    coordinator.config_entry = entry

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
