"""Config flow for KESCO Energy integration."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant import config_entries


from .api import KescoApiClient, KescoAuthError, KescoApiError, async_create_kesco_session
from .const import DOMAIN, CONF_DEBITOR_ID, CONF_AGENCY_ID, DEFAULT_AGENCY_ID

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required("username"): str,
        vol.Required("password"): str,
        vol.Required(CONF_AGENCY_ID, default=DEFAULT_AGENCY_ID): str,
        vol.Required(CONF_DEBITOR_ID): str,
    }
)


class KescoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the KESCO setup UI."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}

        if user_input is not None:
            session = await async_create_kesco_session(self.hass)
            client = KescoApiClient(
                username=user_input["username"],
                password=user_input["password"],
                agency_id=user_input[CONF_AGENCY_ID],
                debitor_id=user_input[CONF_DEBITOR_ID],
                session=session,
            )
            try:
                async with session:
                    await client.async_validate_credentials()
            except KescoAuthError as err:
                _LOGGER.error("KESCO auth error: %s", err)
                errors["base"] = "invalid_auth"
            except KescoApiError as err:
                _LOGGER.error("KESCO connect error: %s", err)
                errors["base"] = "cannot_connect"
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("KESCO unexpected error: %s", err)
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(
                    f"{user_input[CONF_AGENCY_ID]}_{user_input[CONF_DEBITOR_ID]}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"KESCO {user_input[CONF_DEBITOR_ID]}",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )
