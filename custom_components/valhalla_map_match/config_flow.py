"""Config flow for Valhalla Map Match."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_HOST, CONF_PORT, CONF_SSL, DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
        vol.Optional(CONF_SSL, default=False): bool,
    }
)


class ValhallaMapMatchConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Valhalla Map Match."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip().rstrip("/")
            port = user_input[CONF_PORT]
            use_ssl = user_input[CONF_SSL]

            try:
                await self._test_connection(host, port, use_ssl)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected error connecting to Valhalla")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=f"Valhalla ({host}:{port})",
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_SSL: use_ssl,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def _test_connection(self, host: str, port: int, use_ssl: bool) -> None:
        """Verify the Valhalla instance is reachable via its /status endpoint."""
        protocol = "https" if use_ssl else "http"
        url = f"{protocol}://{host}:{port}/status"
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                # Valhalla /status returns 200 when healthy; anything else is a
                # problem we can catch at setup time rather than at call time.
                if resp.status != 200:
                    raise CannotConnect
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect from err


class CannotConnect(Exception):
    """Unable to reach the Valhalla instance."""
