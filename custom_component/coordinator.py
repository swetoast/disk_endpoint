"""DataUpdateCoordinator for Disk & RAID Monitor."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_PORT,
    CONF_USE_HTTPS,
    CONF_VERIFY_SSL,
    DATA_ALERTS,
    DATA_COLLECTED_AT,
    DATA_DISKS,
    DATA_RAIDS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class DiskMonitorCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """
    Polls /system and /alerts from the disk-monitor API.

    Both endpoints are fetched in a single update cycle.  /system provides
    current disk and RAID state; /alerts provides pre-evaluated threshold and
    trend alerts without triggering a hardware probe.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_data: dict[str, Any],
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        self._host = entry_data[CONF_HOST]
        self._port = entry_data[CONF_PORT]
        self._api_key = entry_data.get(CONF_API_KEY, "")
        self._use_https = entry_data.get(CONF_USE_HTTPS, False)
        self._verify_ssl = entry_data.get(CONF_VERIFY_SSL, True)

        scheme = "https" if self._use_https else "http"
        self._base_url = f"{scheme}://{self._host}:{self._port}"

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    async def _fetch(self, session: aiohttp.ClientSession, path: str) -> Any:
        url = f"{self._base_url}{path}"
        ssl = self._verify_ssl if self._use_https else False
        try:
            async with session.get(url, headers=self._headers(), ssl=ssl, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 401:
                    raise UpdateFailed(f"API key required but not provided (401) — check your configuration")
                if resp.status == 403:
                    raise UpdateFailed(f"Invalid API key (403) — check your configuration")
                if resp.status != 200:
                    raise UpdateFailed(f"Unexpected status {resp.status} from {url}")
                return await resp.json()
        except aiohttp.ClientConnectorError as exc:
            raise UpdateFailed(f"Cannot connect to {self._base_url}: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise UpdateFailed(f"Request to {url} failed: {exc}") from exc

    async def _async_update_data(self) -> dict[str, Any]:
        session = async_get_clientsession(self.hass, verify_ssl=self._verify_ssl)

        system, alerts = await asyncio.gather(
            self._fetch(session, "/system"),
            self._fetch(session, "/alerts"),
            return_exceptions=True,
        )

        # If /system fails the whole update fails — we have nothing to show
        if isinstance(system, Exception):
            raise UpdateFailed(f"/system fetch failed: {system}") from system

        # /alerts failing is non-fatal — we keep previous alerts or use empty
        if isinstance(alerts, Exception):
            _LOGGER.warning("Could not fetch /alerts: %s", alerts)
            alerts = {"alerts": [], "criticals": 0, "warnings": 0, "generated_at": 0}

        return {
            DATA_DISKS: system.get("disks", {}),
            DATA_RAIDS: system.get("raids", {}),
            DATA_ALERTS: alerts,
            DATA_COLLECTED_AT: system.get("collected_at", 0),
        }


# asyncio is used in _async_update_data via gather — import it here
import asyncio  # noqa: E402
