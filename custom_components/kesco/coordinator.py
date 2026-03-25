"""DataUpdateCoordinator for KESCO Energy."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import KescoApiClient, KescoApiError, KescoAuthError
from .const import DOMAIN, SCAN_INTERVAL_HOURS

_LOGGER = logging.getLogger(__name__)


class KescoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches invoice and balance data from KESCO on a schedule."""

    def __init__(self, hass: HomeAssistant, client: KescoApiClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=SCAN_INTERVAL_HOURS),
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            invoices = await self.client.async_get_invoices()
        except KescoAuthError as err:
            raise UpdateFailed(f"KESCO authentication error: {err}") from err
        except KescoApiError as err:
            raise UpdateFailed(f"KESCO API error: {err}") from err

        balance: dict[str, Any] = {}
        try:
            balance = await self.client.async_get_balance()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("KESCO balance fetch failed (non-fatal): %s", err)

        notifications: list[dict[str, Any]] = []
        try:
            notifications = await self.client.async_get_notifications()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("KESCO notifications fetch failed (non-fatal): %s", err)

        return {"invoices": invoices, "balance": balance, "notifications": notifications}
