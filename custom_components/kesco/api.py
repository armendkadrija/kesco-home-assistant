"""KESCO Energy API client — handles auth and invoice fetching."""
from __future__ import annotations

import asyncio
import json
import ssl
import time
import logging
from typing import Any

import aiohttp
import certifi

from homeassistant.core import HomeAssistant

from .const import AUTH_URL, BALANCE_URL, INVOICES_URL, NOTIFICATIONS_URL, TOKEN_TTL_SECONDS

_LOGGER = logging.getLogger(__name__)


def _legacy_ssl_context() -> ssl.SSLContext:
    # fatura.kesco-energy.com lacks RFC 5746 secure renegotiation, which OpenSSL 3
    # rejects by default. Allow it for this host only via a dedicated context —
    # never mutate HA's shared (cached) client context.
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    return ctx


async def async_create_kesco_session(hass: HomeAssistant) -> aiohttp.ClientSession:
    """Return a session that can talk to KESCO's legacy TLS server. Caller closes it."""
    ctx = await hass.async_add_executor_job(_legacy_ssl_context)
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))


class KescoAuthError(Exception):
    """Raised when login fails."""


class KescoApiError(Exception):
    """Raised on unexpected API errors."""


class KescoApiClient:
    """Authenticates with KESCO and fetches invoices."""

    def __init__(
        self,
        username: str,
        password: str,
        agency_id: str,
        debitor_id: str,
        session: aiohttp.ClientSession,
    ) -> None:
        self._username = username
        self._password = password
        self._agency_id = agency_id
        self._debitor_id = debitor_id
        self._session = session

        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    async def async_get_invoices(self) -> list[dict[str, Any]]:
        """Return the full invoice list, re-authenticating if needed."""
        await self._ensure_token()
        try:
            return await self._fetch_invoices()
        except KescoAuthError:
            _LOGGER.warning("KESCO invoice 401 — forcing re-login and retrying")
            await self._login()
            return await self._fetch_invoices()

    async def async_get_balance(self) -> dict[str, Any]:
        """Return the account balance dict, re-authenticating if needed."""
        await self._ensure_token()
        try:
            return await self._fetch_balance()
        except KescoAuthError:
            _LOGGER.warning("KESCO balance 401 — forcing re-login and retrying")
            await self._login()
            return await self._fetch_balance()

    async def async_get_notifications(self, page_size: int = 10) -> list[dict[str, Any]]:
        """Return the latest notifications, re-authenticating if needed."""
        await self._ensure_token()
        try:
            return await self._fetch_notifications(page_size)
        except KescoAuthError:
            _LOGGER.warning("KESCO notifications 401 — forcing re-login and retrying")
            await self._login()
            return await self._fetch_notifications(page_size)

    async def async_validate_credentials(self) -> None:
        """Login once to verify credentials — raises KescoAuthError on failure."""
        await self._login()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _ensure_token(self) -> None:
        if self._token is None or time.monotonic() >= self._token_expires_at:
            _LOGGER.debug("KESCO token missing or expired — re-authenticating")
            await self._login()

    async def _login(self) -> None:
        payload = {
            "Username": self._username,
            "Password": self._password,
            "RememberMe": True,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            async with self._session.post(
                AUTH_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                raw = await resp.text()
                _LOGGER.debug("KESCO auth response HTTP %s: %s", resp.status, raw[:200])
                if resp.status == 401:
                    raise KescoAuthError("Invalid KESCO credentials")
                if resp.status != 200:
                    raise KescoApiError(
                        f"Auth endpoint returned HTTP {resp.status}: {raw[:200]}"
                    )
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as err:
                    raise KescoApiError(
                        f"Auth response is not JSON: {raw[:200]}"
                    ) from err
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("KESCO login failed: %s: %s", type(err).__name__, err)
            raise KescoApiError(f"Network error during login: {err}") from err

        # Response: {"State": 1, "Data": {"accessToken": "eyJ..."}}
        state = data.get("State")
        if state != 1:
            raise KescoAuthError(
                f"Login rejected by KESCO (State={state})"
            )

        token = (data.get("Data") or {}).get("accessToken")
        if not token:
            raise KescoApiError("No accessToken in login response")

        self._token = token
        self._token_expires_at = time.monotonic() + TOKEN_TTL_SECONDS
        _LOGGER.debug("KESCO authenticated successfully")

    async def _fetch_invoices(self) -> list[dict[str, Any]]:
        params = {
            "agencyId": self._agency_id,
            "debitorId": self._debitor_id,
        }
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        try:
            async with self._session.get(
                INVOICES_URL,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                raw = await resp.text()
                _LOGGER.debug("KESCO invoices response HTTP %s: %s", resp.status, raw[:200])
                if resp.status == 401:
                    self._token = None
                    raise KescoAuthError("Bearer token rejected by invoice API")
                if resp.status != 200:
                    raise KescoApiError(
                        f"Invoice endpoint returned HTTP {resp.status}: {raw[:200]}"
                    )
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as err:
                    raise KescoApiError(
                        f"Invoice response is not JSON: {raw[:200]}"
                    ) from err
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("KESCO invoice fetch failed: %s: %s", type(err).__name__, err)
            raise KescoApiError(f"Network error fetching invoices: {err}") from err

        # Response: {"State": 1, "Data": {"invoices": [...], "lastUpdate": "..."}}
        state = data.get("State")
        if state != 1:
            raise KescoApiError(f"Invoice API returned State={state}")

        invoices = (data.get("Data") or {}).get("invoices")
        if not isinstance(invoices, list):
            raise KescoApiError(f"No invoices list in response: {str(data)[:200]}")

        # Sort descending so index 0 = latest
        invoices.sort(
            key=lambda inv: (
                inv.get("BillingPeriodYear", 0),
                inv.get("BillingPeriodID", 0),
            ),
            reverse=True,
        )
        return invoices

    async def _fetch_balance(self) -> dict[str, Any]:
        params = {
            "agencyId": self._agency_id,
            "debitorId": self._debitor_id,
        }
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        try:
            async with self._session.get(
                BALANCE_URL,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                raw = await resp.text()
                _LOGGER.debug("KESCO balance response HTTP %s: %s", resp.status, raw[:200])
                if resp.status == 401:
                    self._token = None
                    raise KescoAuthError("Bearer token rejected by balance API")
                if resp.status != 200:
                    raise KescoApiError(
                        f"Balance endpoint returned HTTP {resp.status}: {raw[:200]}"
                    )
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as err:
                    raise KescoApiError(
                        f"Balance response is not JSON: {raw[:200]}"
                    ) from err
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("KESCO balance fetch failed: %s: %s", type(err).__name__, err)
            raise KescoApiError(f"Network error fetching balance: {err}") from err

        state = data.get("State")
        if state != 1:
            raise KescoApiError(f"Balance API returned State={state}")

        balance = (data.get("Data") or {}).get("balance")
        if not isinstance(balance, dict):
            raise KescoApiError(f"No balance object in response: {str(data)[:200]}")

        return balance

    async def _fetch_notifications(self, page_size: int) -> list[dict[str, Any]]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        params = {"pageSize": page_size, "pageNumber": 0}
        try:
            async with self._session.get(
                NOTIFICATIONS_URL,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                raw = await resp.text()
                _LOGGER.debug("KESCO notifications response HTTP %s: %s", resp.status, raw[:200])
                if resp.status == 401:
                    self._token = None
                    raise KescoAuthError("Bearer token rejected by notifications API")
                if resp.status != 200:
                    raise KescoApiError(
                        f"Notifications endpoint returned HTTP {resp.status}: {raw[:200]}"
                    )
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as err:
                    raise KescoApiError(
                        f"Notifications response is not JSON: {raw[:200]}"
                    ) from err
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("KESCO notifications fetch failed: %s: %s", type(err).__name__, err)
            raise KescoApiError(f"Network error fetching notifications: {err}") from err

        state = data.get("State")
        if state != 1:
            raise KescoApiError(f"Notifications API returned State={state}")

        notifications = data.get("Data")
        if not isinstance(notifications, list):
            raise KescoApiError(f"No notifications list in response: {str(data)[:200]}")

        return notifications
