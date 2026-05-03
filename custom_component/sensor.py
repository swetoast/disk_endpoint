"""Sensor entities for Disk & RAID Monitor."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_ALERTS, DATA_DISKS, DATA_RAIDS, DOMAIN
from .coordinator import DiskMonitorCoordinator


# ─────────────────────────────────────────────────────────────────────────────
# Disk sensor descriptions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, kw_only=True)
class DiskSensorEntityDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    # When True the sensor is only created if value_fn returns non-None on the
    # first data fetch (used to skip ATA-only attrs on NVMe and vice-versa)
    requires_value: bool = False


DISK_SENSORS: tuple[DiskSensorEntityDescription, ...] = (
    DiskSensorEntityDescription(
        key="temperature",
        name="Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("smart", {}).get("temperature_c"),
    ),
    DiskSensorEntityDescription(
        key="power_on_hours",
        name="Power-on hours",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:clock-outline",
        value_fn=lambda d: d.get("smart", {}).get("power_on_hours"),
    ),
    DiskSensorEntityDescription(
        key="reallocated_sectors",
        name="Reallocated sectors",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:harddisk-remove",
        value_fn=lambda d: d.get("smart", {}).get("reallocated_sectors"),
        requires_value=True,
    ),
    DiskSensorEntityDescription(
        key="pending_sectors",
        name="Pending sectors",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:harddisk-plus",
        value_fn=lambda d: d.get("smart", {}).get("pending_sectors"),
        requires_value=True,
    ),
    DiskSensorEntityDescription(
        key="uncorrectable_errors",
        name="Uncorrectable errors",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:alert-circle-outline",
        value_fn=lambda d: d.get("smart", {}).get("uncorrectable_errors"),
        requires_value=True,
    ),
    # NVMe-only
    DiskSensorEntityDescription(
        key="nvme_wear",
        name="NVMe wear",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:chip",
        value_fn=lambda d: (d.get("smart", {}).get("nvme") or {}).get("percentage_used"),
        requires_value=True,
    ),
    DiskSensorEntityDescription(
        key="nvme_available_spare",
        name="NVMe available spare",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:chip",
        value_fn=lambda d: (d.get("smart", {}).get("nvme") or {}).get("available_spare"),
        requires_value=True,
    ),
    DiskSensorEntityDescription(
        key="nvme_media_errors",
        name="NVMe media errors",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:chip",
        value_fn=lambda d: (d.get("smart", {}).get("nvme") or {}).get("media_errors"),
        requires_value=True,
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# RAID sensor descriptions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, kw_only=True)
class RaidSensorEntityDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]


RAID_SENSORS: tuple[RaidSensorEntityDescription, ...] = (
    RaidSensorEntityDescription(
        key="state",
        name="State",
        icon="mdi:nas",
        value_fn=lambda r: r.get("state"),
    ),
    RaidSensorEntityDescription(
        key="active_devices",
        name="Active devices",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:harddisk",
        value_fn=lambda r: r.get("active_devices"),
    ),
    RaidSensorEntityDescription(
        key="failed_devices",
        name="Failed devices",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:harddisk-remove",
        value_fn=lambda r: r.get("failed_devices"),
    ),
    RaidSensorEntityDescription(
        key="spare_devices",
        name="Spare devices",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:harddisk-plus",
        value_fn=lambda r: r.get("spare_devices"),
    ),
    RaidSensorEntityDescription(
        key="resync_percent",
        name="Resync progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:sync",
        value_fn=lambda r: r.get("resync_percent"),
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# Alert summary sensors (global, not per-device)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, kw_only=True)
class AlertSensorEntityDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]


ALERT_SENSORS: tuple[AlertSensorEntityDescription, ...] = (
    AlertSensorEntityDescription(
        key="critical_alerts",
        name="Critical alerts",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:alert",
        value_fn=lambda a: a.get("criticals", 0),
    ),
    AlertSensorEntityDescription(
        key="warning_alerts",
        name="Warning alerts",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:alert-outline",
        value_fn=lambda a: a.get("warnings", 0),
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

    entities: list[SensorEntity] = []

    # Disk sensors
    for disk_name, disk_data in coordinator.data[DATA_DISKS].items():
        for desc in DISK_SENSORS:
            if desc.requires_value and desc.value_fn(disk_data) is None:
                continue
            entities.append(DiskSensor(coordinator, entry, disk_name, disk_data, desc))

    # RAID sensors
    for raid_name, raid_data in coordinator.data[DATA_RAIDS].items():
        for desc in RAID_SENSORS:
            entities.append(RaidSensor(coordinator, entry, raid_name, raid_data, desc))

    # Global alert sensors
    for desc in ALERT_SENSORS:
        entities.append(AlertSensor(coordinator, entry, desc))

    async_add_entities(entities)


# ─────────────────────────────────────────────────────────────────────────────
# Entity classes
# ─────────────────────────────────────────────────────────────────────────────

class DiskSensor(CoordinatorEntity[DiskMonitorCoordinator], SensorEntity):
    """A sensor for one numeric attribute of one physical disk."""

    entity_description: DiskSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DiskMonitorCoordinator,
        entry: ConfigEntry,
        disk_name: str,
        disk_data: dict[str, Any],
        description: DiskSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._disk_name = disk_name
        self._attr_unique_id = f"{entry.entry_id}_disk_{disk_name}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_disk_{disk_name}")},
            name=f"Disk {disk_name}",
            manufacturer=_manufacturer(disk_data),
            model=disk_data.get("model"),
            sw_version=disk_data.get("firmware"),
            serial_number=disk_data.get("serial"),
            via_device=(DOMAIN, entry.entry_id),
        )

    @property
    def _disk(self) -> dict[str, Any]:
        return self.coordinator.data[DATA_DISKS].get(self._disk_name, {})

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self._disk)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        disk = self._disk
        return {
            "disk_type": disk.get("disk_type"),
            "transport": disk.get("transport"),
            "size_bytes": disk.get("size_bytes"),
            "raid_member_of": disk.get("raid_member_of"),
        }


class RaidSensor(CoordinatorEntity[DiskMonitorCoordinator], SensorEntity):
    """A sensor for one attribute of one RAID array."""

    entity_description: RaidSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DiskMonitorCoordinator,
        entry: ConfigEntry,
        raid_name: str,
        raid_data: dict[str, Any],
        description: RaidSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._raid_name = raid_name
        self._attr_unique_id = f"{entry.entry_id}_raid_{raid_name}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_raid_{raid_name}")},
            name=f"RAID {raid_name}",
            model=raid_data.get("level", "md RAID").upper(),
            via_device=(DOMAIN, entry.entry_id),
        )

    @property
    def _raid(self) -> dict[str, Any]:
        return self.coordinator.data[DATA_RAIDS].get(self._raid_name, {})

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self._raid)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        raid = self._raid
        members = raid.get("members", [])
        return {
            "level": raid.get("level"),
            "total_devices": raid.get("total_devices"),
            "working_devices": raid.get("working_devices"),
            "chunk_size_kb": raid.get("chunk_size_kb"),
            "size_bytes": raid.get("size_bytes"),
            "members": [
                {"name": m["name"], "state": m["state"], "slot": m.get("slot")}
                for m in members
            ],
        }


class AlertSensor(CoordinatorEntity[DiskMonitorCoordinator], SensorEntity):
    """A global sensor reporting the count of critical or warning alerts."""

    entity_description: AlertSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DiskMonitorCoordinator,
        entry: ConfigEntry,
        description: AlertSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Disk Monitor",
            entry_type="service",
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data[DATA_ALERTS])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        alerts = self.coordinator.data[DATA_ALERTS].get("alerts", [])
        level_filter = "critical" if "critical" in self.entity_description.key else "warning"
        return {
            "alerts": [
                {
                    "device": a["device"],
                    "device_type": a["device_type"],
                    "message": a["message"],
                    "value": a.get("value"),
                }
                for a in alerts
                if a.get("level") == level_filter
            ]
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _manufacturer(disk_data: dict[str, Any]) -> str | None:
    """Guess manufacturer from model string."""
    model = (disk_data.get("model") or "").lower()
    if "samsung" in model:
        return "Samsung"
    if "western digital" in model or model.startswith("wdc") or model.startswith("wd"):
        return "Western Digital"
    if "seagate" in model or model.startswith("st"):
        return "Seagate"
    if "toshiba" in model:
        return "Toshiba"
    if "hitachi" in model or "hgst" in model:
        return "HGST"
    if "intel" in model:
        return "Intel"
    if "crucial" in model or "micron" in model:
        return "Crucial / Micron"
    if "kingston" in model:
        return "Kingston"
    if "sandisk" in model:
        return "SanDisk"
    return None
