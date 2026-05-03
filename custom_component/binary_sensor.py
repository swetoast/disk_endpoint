"""Binary sensor entities for Disk & RAID Monitor."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_ALERTS, DATA_DISKS, DATA_RAIDS, DOMAIN
from .coordinator import DiskMonitorCoordinator


# ─────────────────────────────────────────────────────────────────────────────
# Disk binary sensor descriptions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, kw_only=True)
class DiskBinarySensorEntityDescription(BinarySensorEntityDescription):
    # Returns True when the sensor should be "on" (i.e. problem detected)
    is_on_fn: Callable[[dict[str, Any]], bool]


DISK_BINARY_SENSORS: tuple[DiskBinarySensorEntityDescription, ...] = (
    DiskBinarySensorEntityDescription(
        key="smart_health",
        name="SMART health",
        device_class=BinarySensorDeviceClass.PROBLEM,
        # On = problem = SMART failed. None (unknown) is treated as no problem.
        is_on_fn=lambda d: d.get("smart", {}).get("passed") is False,
    ),
    DiskBinarySensorEntityDescription(
        key="has_reallocated_sectors",
        name="Reallocated sectors",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda d: (d.get("smart", {}).get("reallocated_sectors") or 0) > 0,
    ),
    DiskBinarySensorEntityDescription(
        key="has_pending_sectors",
        name="Pending sectors",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda d: (d.get("smart", {}).get("pending_sectors") or 0) > 0,
    ),
    DiskBinarySensorEntityDescription(
        key="has_uncorrectable_errors",
        name="Uncorrectable errors",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda d: (d.get("smart", {}).get("uncorrectable_errors") or 0) > 0,
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# RAID binary sensor descriptions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, kw_only=True)
class RaidBinarySensorEntityDescription(BinarySensorEntityDescription):
    is_on_fn: Callable[[dict[str, Any]], bool]


RAID_BINARY_SENSORS: tuple[RaidBinarySensorEntityDescription, ...] = (
    RaidBinarySensorEntityDescription(
        key="degraded",
        name="Degraded",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda r: r.get("state") not in ("clean", None),
    ),
    RaidBinarySensorEntityDescription(
        key="has_failed_devices",
        name="Failed devices",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda r: (r.get("failed_devices") or 0) > 0,
    ),
    RaidBinarySensorEntityDescription(
        key="no_spare",
        name="No hot spare",
        device_class=BinarySensorDeviceClass.PROBLEM,
        # On = problem = redundant array with zero spares configured
        is_on_fn=lambda r: (
            (r.get("spare_devices") or 0) == 0
            and (r.get("total_devices") or 0) > 1
            and "raid0" not in (r.get("level") or "")
            and "linear" not in (r.get("level") or "")
        ),
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# Global alert binary sensors
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, kw_only=True)
class AlertBinarySensorEntityDescription(BinarySensorEntityDescription):
    is_on_fn: Callable[[dict[str, Any]], bool]


ALERT_BINARY_SENSORS: tuple[AlertBinarySensorEntityDescription, ...] = (
    AlertBinarySensorEntityDescription(
        key="has_critical_alerts",
        name="Has critical alerts",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda a: (a.get("criticals") or 0) > 0,
    ),
    AlertBinarySensorEntityDescription(
        key="has_warning_alerts",
        name="Has warning alerts",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda a: (a.get("warnings") or 0) > 0,
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# Entity setup
# ─────────────────────────────────────────────────────────────────────────────

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DiskMonitorCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[BinarySensorEntity] = []

    for disk_name, disk_data in coordinator.data[DATA_DISKS].items():
        for desc in DISK_BINARY_SENSORS:
            entities.append(DiskBinarySensor(coordinator, entry, disk_name, disk_data, desc))

    for raid_name, raid_data in coordinator.data[DATA_RAIDS].items():
        for desc in RAID_BINARY_SENSORS:
            entities.append(RaidBinarySensor(coordinator, entry, raid_name, raid_data, desc))

    for desc in ALERT_BINARY_SENSORS:
        entities.append(AlertBinarySensor(coordinator, entry, desc))

    async_add_entities(entities)


# ─────────────────────────────────────────────────────────────────────────────
# Entity classes
# ─────────────────────────────────────────────────────────────────────────────

class DiskBinarySensor(CoordinatorEntity[DiskMonitorCoordinator], BinarySensorEntity):
    """A binary sensor representing a fault condition on one physical disk."""

    entity_description: DiskBinarySensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DiskMonitorCoordinator,
        entry: ConfigEntry,
        disk_name: str,
        disk_data: dict[str, Any],
        description: DiskBinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._disk_name = disk_name
        self._attr_unique_id = f"{entry.entry_id}_disk_{disk_name}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_disk_{disk_name}")},
            name=f"Disk {disk_name}",
            via_device=(DOMAIN, entry.entry_id),
        )

    @property
    def is_on(self) -> bool:
        disk = self.coordinator.data[DATA_DISKS].get(self._disk_name, {})
        return self.entity_description.is_on_fn(disk)


class RaidBinarySensor(CoordinatorEntity[DiskMonitorCoordinator], BinarySensorEntity):
    """A binary sensor representing a fault condition on one RAID array."""

    entity_description: RaidBinarySensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DiskMonitorCoordinator,
        entry: ConfigEntry,
        raid_name: str,
        raid_data: dict[str, Any],
        description: RaidBinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._raid_name = raid_name
        self._attr_unique_id = f"{entry.entry_id}_raid_{raid_name}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_raid_{raid_name}")},
            name=f"RAID {raid_name}",
            via_device=(DOMAIN, entry.entry_id),
        )

    @property
    def is_on(self) -> bool:
        raid = self.coordinator.data[DATA_RAIDS].get(self._raid_name, {})
        return self.entity_description.is_on_fn(raid)


class AlertBinarySensor(CoordinatorEntity[DiskMonitorCoordinator], BinarySensorEntity):
    """A global binary sensor that turns on when alerts of a given level exist."""

    entity_description: AlertBinarySensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DiskMonitorCoordinator,
        entry: ConfigEntry,
        description: AlertBinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Disk Monitor",
            entry_type="service",
        )

    @property
    def is_on(self) -> bool:
        return self.entity_description.is_on_fn(self.coordinator.data[DATA_ALERTS])
