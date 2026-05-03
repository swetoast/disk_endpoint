"""Config flow for Disk & RAID Monitor."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_PORT,
    CONF_USE_HTTPS,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_USE_HTTPS,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.All(int, vol.Range(min=1, max=65535)),
        vol.Optional(CONF_API_KEY, default=""): str,
        vol.Optional(CONF_USE_HTTPS, default=DEFAULT_USE_HTTPS): bool,
        vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
            int, vol.Range(min=10, max=3600)
        ),
    }
)


async def _test_connection(
    hass: Any,
    host: str,
    port: int,
    api_key: str,
    use_https: bool,
    verify_ssl: bool,
) -> str | None:
    """
    Attempt a GET /health against the API.

    Returns None on success, or an error key string that maps to a
    strings.json translation on failure.
    """
    scheme = "https" if use_https else "http"
    url = f"{scheme}://{host}:{port}/health"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    try:
        async with session.get(
            url,
            headers=headers,
            ssl=verify_ssl if use_https else False,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 401:
                return "invalid_auth"
            if resp.status == 403:
                return "invalid_auth"
            if resp.status != 200:
                return "cannot_connect"
            data = await resp.json()
            if data.get("status") != "ok":
                return "cannot_connect"
    except aiohttp.ClientConnectorError:
        return "cannot_connect"
    except aiohttp.ClientError:
        return "cannot_connect"
    except Exception:  # noqa: BLE001
        return "unknown"

    return None


class DiskMonitorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI setup flow for Disk & RAID Monitor."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip().rstrip("/")
            port = user_input[CONF_PORT]
            api_key = user_input.get(CONF_API_KEY, "").strip()
            use_https = user_input.get(CONF_USE_HTTPS, DEFAULT_USE_HTTPS)
            verify_ssl = user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)

            # Prevent duplicate entries for the same host:port
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            error_key = await _test_connection(
                self.hass, host, port, api_key, use_https, verify_ssl
            )

            if error_key is None:
                return self.async_create_entry(
                    title=f"Disk Monitor ({host}:{port})",
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_API_KEY: api_key,
                        CONF_USE_HTTPS: use_https,
                        CONF_VERIFY_SSL: verify_ssl,
                        CONF_SCAN_INTERVAL: user_input.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                    },
                )

            errors["base"] = error_key

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )
