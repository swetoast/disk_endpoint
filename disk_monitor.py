"""
disk_monitor.py — Disk & RAID Monitor API with History and Alerts
=================================================================
Extends the probe pipeline with:

  APScheduler   Runs collect_and_save() on a configurable interval so the
                database grows automatically without waiting for API requests.

  SQLite (WAL)  Stores every snapshot. WAL mode lets the API serve reads
                concurrently while the scheduler writes. No extra services.

  Trend analysis  At query time the DB computes how key attributes have changed
                 between the earliest and most recent snapshot for each disk.
                 Rising reallocated-sector counts or accelerating NVMe wear are
                 caught before the drive fails.

  Alerts endpoint  /alerts queries the DB only — it never triggers a probe —
                  so it is always fast. Alerts cover threshold violations and
                  cross-snapshot trend detections.

Pipeline (same four steps):
  1. lsblk --json          physical disk list
  2. /proc/mdstat          RAID arrays + resync progress
  3. mdadm --detail        authoritative RAID state + member states
  4. smartctl -a --json    SMART health, attributes, NVMe log, identity

Run:   uvicorn disk_monitor:app --host 0.0.0.0 --port 8000
Docs:  http://host:port/docs
"""

from __future__ import annotations

import configparser
import contextlib
import json
import logging
import re
import sqlite3
import subprocess
import threading
import time
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Generator

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query, Security
from fastapi.security.api_key import APIKeyHeader, APIKeyQuery
from pydantic import BaseModel, Field

log = logging.getLogger("disk_monitor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).with_name("disk_monitor.conf")

_DEFAULTS: dict[str, str] = {
    "HOST": "0.0.0.0",
    "PORT": "8000",
    "USE_HTTPS": "false",
    "CERTIFICATE_PATH": "",
    "KEY_PATH": "",
    "TOKEN": "",
    "CACHE_TTL": "60",
    "DB_PATH": "disk_monitor.db",
    "SCHEDULE_INTERVAL_MINUTES": "15",
    "ALERT_TEMP_WARNING_C": "55",
    "ALERT_TEMP_CRITICAL_C": "65",
    "ALERT_NVME_WEAR_WARNING_PCT": "80",
    "ALERT_NVME_WEAR_CRITICAL_PCT": "95",
}


class AppConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    use_https: bool = False
    certificate_path: str = ""
    key_path: str = ""
    token: str = ""
    cache_ttl: int = 60
    db_path: str = "disk_monitor.db"
    schedule_interval_minutes: int = 15
    alert_temp_warning_c: int = 55
    alert_temp_critical_c: int = 65
    alert_nvme_wear_warning_pct: int = 80
    alert_nvme_wear_critical_pct: int = 95


@lru_cache(maxsize=1)
def load_config() -> AppConfig:
    cfg = configparser.ConfigParser(defaults=_DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.read(CONFIG_PATH)
    s = cfg["DEFAULT"]
    return AppConfig(
        host=s["HOST"],
        port=int(s["PORT"]),
        use_https=s.getboolean("USE_HTTPS"),
        certificate_path=s["CERTIFICATE_PATH"],
        key_path=s["KEY_PATH"],
        token=s["TOKEN"],
        cache_ttl=int(s["CACHE_TTL"]),
        db_path=s["DB_PATH"],
        schedule_interval_minutes=int(s["SCHEDULE_INTERVAL_MINUTES"]),
        alert_temp_warning_c=int(s["ALERT_TEMP_WARNING_C"]),
        alert_temp_critical_c=int(s["ALERT_TEMP_CRITICAL_C"]),
        alert_nvme_wear_warning_pct=int(s["ALERT_NVME_WEAR_WARNING_PCT"]),
        alert_nvme_wear_critical_pct=int(s["ALERT_NVME_WEAR_CRITICAL_PCT"]),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Authentication
#
# API keys are accepted in two places (checked in order):
#
#   1. X-API-Key request header  — preferred; never appears in server logs or
#                                   browser history.
#   2. ?token= query parameter   — fallback for quick curl / browser testing
#                                   only. Avoid in production.
#
# If TOKEN is empty in disk_monitor.conf, authentication is disabled entirely
# and all endpoints are reachable without a key.
# ─────────────────────────────────────────────────────────────────────────────

_header_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)
_query_scheme  = APIKeyQuery(name="token",      auto_error=False)


def require_token(
    header_key: str | None = Security(_header_scheme),
    query_key:  str | None = Security(_query_scheme),
) -> None:
    """
    Validate the API key supplied by the caller.

    Precedence: X-API-Key header > ?token= query param.
    Raises HTTP 401 when no key is provided and one is required.
    Raises HTTP 403 when a key is provided but does not match.
    """
    required = load_config().token
    if not required:
        return  # auth disabled

    provided = header_key or query_key

    if provided is None:
        raise HTTPException(
            status_code=401,
            detail="API key required. Supply it in the X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    if provided != required:
        raise HTTPException(
            status_code=403,
            detail="Invalid API key.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Subprocess helper
# ─────────────────────────────────────────────────────────────────────────────

def _cmd(args: list[str], timeout: int = 30) -> tuple[str, str | None]:
    """
    Run *args*. Returns (stdout, None) on success, ("", reason) on failure.

    smartctl uses a bitmask exit code — non-zero does NOT mean failure.
    We return stdout whenever it is present, regardless of exit code.
    """
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if result.stdout.strip():
            return result.stdout, None
        stderr = result.stderr.strip()
        return "", stderr or f"exited {result.returncode} with no output"
    except FileNotFoundError:
        return "", f"command not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return "", f"timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return "", str(exc)


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models — probes
# ─────────────────────────────────────────────────────────────────────────────

class ProbeError(BaseModel):
    source: str
    message: str


class SmartAttribute(BaseModel):
    id: int
    name: str
    value: int
    worst: int
    threshold: int
    raw_value: int
    failed: bool


class NvmeHealth(BaseModel):
    percentage_used: int | None = None
    media_errors: int | None = None
    unsafe_shutdowns: int | None = None
    power_cycles: int | None = None
    available_spare: int | None = None
    available_spare_threshold: int | None = None
    critical_warning: int | None = None


class SmartInfo(BaseModel):
    passed: bool | None = None
    temperature_c: int | None = None
    power_on_hours: int | None = None
    reallocated_sectors: int | None = None
    pending_sectors: int | None = None
    uncorrectable_errors: int | None = None
    attributes: list[SmartAttribute] = Field(default_factory=list)
    nvme: NvmeHealth | None = None


class DiskInfo(BaseModel):
    name: str
    path: str
    disk_type: str
    transport: str | None = None
    size_bytes: int | None = None
    model: str | None = None
    serial: str | None = None
    firmware: str | None = None
    rpm: int | None = None
    raid_member_of: str | None = None
    smart: SmartInfo = Field(default_factory=SmartInfo)
    errors: list[ProbeError] = Field(default_factory=list)


class RaidMember(BaseModel):
    name: str
    path: str
    slot: int | None = None
    state: str


class RaidInfo(BaseModel):
    name: str
    path: str
    level: str | None = None
    state: str
    size_bytes: int | None = None
    chunk_size_kb: int | None = None
    total_devices: int | None = None
    active_devices: int | None = None
    working_devices: int | None = None
    failed_devices: int | None = None
    spare_devices: int | None = None
    resync_percent: float | None = None
    resync_speed_mib: float | None = None
    members: list[RaidMember] = Field(default_factory=list)
    errors: list[ProbeError] = Field(default_factory=list)


class SystemOverview(BaseModel):
    disks: dict[str, DiskInfo]
    raids: dict[str, RaidInfo]
    collected_at: float
    errors: list[ProbeError] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models — history and alerts
# ─────────────────────────────────────────────────────────────────────────────

class DiskSnapshot(BaseModel):
    snapshot_id: int
    collected_at: float
    smart_passed: bool | None
    temperature_c: int | None
    power_on_hours: int | None
    reallocated_sectors: int | None
    pending_sectors: int | None
    uncorrectable_errors: int | None
    nvme_percentage_used: int | None
    nvme_media_errors: int | None
    nvme_available_spare: int | None
    nvme_critical_warning: int | None


class DiskTrend(BaseModel):
    """Delta between the oldest and newest snapshot in the queried window."""
    reallocated_sectors_delta: int | None = Field(
        None, description="Sectors reallocated since first snapshot (>0 means active failure)"
    )
    pending_sectors_delta: int | None = None
    uncorrectable_errors_delta: int | None = None
    nvme_wear_delta: int | None = Field(
        None, description="NVMe percentage_used increase since first snapshot"
    )
    nvme_media_errors_delta: int | None = None
    temperature_max: int | None = None
    temperature_min: int | None = None


class DiskHistory(BaseModel):
    name: str
    model: str | None
    serial: str | None
    disk_type: str | None
    total_snapshots: int
    snapshots: list[DiskSnapshot]
    trend: DiskTrend


class RaidSnapshot(BaseModel):
    snapshot_id: int
    collected_at: float
    state: str
    active_devices: int | None
    failed_devices: int | None
    spare_devices: int | None
    resync_percent: float | None


class RaidHistory(BaseModel):
    name: str
    level: str | None
    total_snapshots: int
    snapshots: list[RaidSnapshot]
    times_degraded: int = Field(
        ..., description="Snapshots where state was not clean (full history, not just the window)"
    )


class AlertLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Alert(BaseModel):
    level: AlertLevel
    device: str
    device_type: str = Field(..., description="disk | raid")
    message: str
    value: Any = Field(None, description="The value that triggered the alert")
    detected_at: float


class AlertsSummary(BaseModel):
    alerts: list[Alert]
    generated_at: float
    criticals: int
    warnings: int


# ─────────────────────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _db_conn() -> Generator[sqlite3.Connection, None, None]:
    """
    Short-lived SQLite connection in WAL mode.

    WAL allows multiple concurrent API readers alongside the scheduler's writer
    without Python-level locking. A new connection is created per operation.
    """
    conn = sqlite3.connect(load_config().db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create all tables and indices if they don't exist. Safe to call every startup."""
    with _db_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS disk_snapshots (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                collected_at         REAL    NOT NULL,
                name                 TEXT    NOT NULL,
                path                 TEXT    NOT NULL,
                disk_type            TEXT,
                transport            TEXT,
                size_bytes           INTEGER,
                model                TEXT,
                serial               TEXT,
                firmware             TEXT,
                rpm                  INTEGER,
                raid_member_of       TEXT,
                smart_passed         INTEGER,
                temperature_c        INTEGER,
                power_on_hours       INTEGER,
                reallocated_sectors  INTEGER,
                pending_sectors      INTEGER,
                uncorrectable_errors INTEGER
            );

            CREATE TABLE IF NOT EXISTS smart_attributes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL
                            REFERENCES disk_snapshots(id) ON DELETE CASCADE,
                attr_id     INTEGER NOT NULL,
                name        TEXT    NOT NULL,
                value       INTEGER,
                worst       INTEGER,
                threshold   INTEGER,
                raw_value   INTEGER,
                failed      INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS nvme_health_snapshots (
                id                        INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id               INTEGER NOT NULL
                                          REFERENCES disk_snapshots(id) ON DELETE CASCADE,
                percentage_used           INTEGER,
                media_errors              INTEGER,
                unsafe_shutdowns          INTEGER,
                power_cycles              INTEGER,
                available_spare           INTEGER,
                available_spare_threshold INTEGER,
                critical_warning          INTEGER
            );

            CREATE TABLE IF NOT EXISTS raid_snapshots (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                collected_at     REAL    NOT NULL,
                name             TEXT    NOT NULL,
                path             TEXT    NOT NULL,
                level            TEXT,
                state            TEXT    NOT NULL,
                size_bytes       INTEGER,
                chunk_size_kb    INTEGER,
                total_devices    INTEGER,
                active_devices   INTEGER,
                working_devices  INTEGER,
                failed_devices   INTEGER,
                spare_devices    INTEGER,
                resync_percent   REAL,
                resync_speed_mib REAL
            );

            CREATE TABLE IF NOT EXISTS raid_member_snapshots (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                raid_snapshot_id INTEGER NOT NULL
                                 REFERENCES raid_snapshots(id) ON DELETE CASCADE,
                name             TEXT    NOT NULL,
                path             TEXT,
                slot             INTEGER,
                state            TEXT
            );

            CREATE TABLE IF NOT EXISTS probe_errors (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                collected_at REAL    NOT NULL,
                source       TEXT    NOT NULL,
                message      TEXT    NOT NULL,
                disk_name    TEXT,
                raid_name    TEXT
            );

            -- Fast lookups for history and alert queries
            CREATE INDEX IF NOT EXISTS idx_disk_snap_name_ts
                ON disk_snapshots (name, collected_at DESC);

            CREATE INDEX IF NOT EXISTS idx_raid_snap_name_ts
                ON raid_snapshots (name, collected_at DESC);

            CREATE INDEX IF NOT EXISTS idx_errors_ts
                ON probe_errors (collected_at DESC);
        """)
    log.info("Database ready at '%s'", load_config().db_path)


def save_snapshot(overview: SystemOverview) -> None:
    """Persist one SystemOverview to the database in a single transaction."""
    ts = overview.collected_at

    with _db_conn() as conn:
        # ── Disks ─────────────────────────────────────────────────────────
        for disk in overview.disks.values():
            s = disk.smart
            passed = None if s.passed is None else int(s.passed)

            cur = conn.execute(
                """
                INSERT INTO disk_snapshots (
                    collected_at, name, path, disk_type, transport,
                    size_bytes, model, serial, firmware, rpm, raid_member_of,
                    smart_passed, temperature_c, power_on_hours,
                    reallocated_sectors, pending_sectors, uncorrectable_errors
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ts, disk.name, disk.path, disk.disk_type, disk.transport,
                    disk.size_bytes, disk.model, disk.serial, disk.firmware,
                    disk.rpm, disk.raid_member_of,
                    passed, s.temperature_c, s.power_on_hours,
                    s.reallocated_sectors, s.pending_sectors, s.uncorrectable_errors,
                ),
            )
            snap_id = cur.lastrowid

            if s.attributes:
                conn.executemany(
                    """
                    INSERT INTO smart_attributes
                        (snapshot_id, attr_id, name, value, worst, threshold, raw_value, failed)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    [
                        (snap_id, a.id, a.name, a.value, a.worst, a.threshold,
                         a.raw_value, int(a.failed))
                        for a in s.attributes
                    ],
                )

            if s.nvme:
                n = s.nvme
                conn.execute(
                    """
                    INSERT INTO nvme_health_snapshots (
                        snapshot_id, percentage_used, media_errors, unsafe_shutdowns,
                        power_cycles, available_spare, available_spare_threshold, critical_warning
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (snap_id, n.percentage_used, n.media_errors, n.unsafe_shutdowns,
                     n.power_cycles, n.available_spare, n.available_spare_threshold,
                     n.critical_warning),
                )

            for err in disk.errors:
                conn.execute(
                    "INSERT INTO probe_errors (collected_at, source, message, disk_name) VALUES (?,?,?,?)",
                    (ts, err.source, err.message, disk.name),
                )

        # ── RAID arrays ───────────────────────────────────────────────────
        for raid in overview.raids.values():
            cur = conn.execute(
                """
                INSERT INTO raid_snapshots (
                    collected_at, name, path, level, state, size_bytes,
                    chunk_size_kb, total_devices, active_devices, working_devices,
                    failed_devices, spare_devices, resync_percent, resync_speed_mib
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ts, raid.name, raid.path, raid.level, raid.state,
                    raid.size_bytes, raid.chunk_size_kb, raid.total_devices,
                    raid.active_devices, raid.working_devices, raid.failed_devices,
                    raid.spare_devices, raid.resync_percent, raid.resync_speed_mib,
                ),
            )
            snap_id = cur.lastrowid

            if raid.members:
                conn.executemany(
                    """
                    INSERT INTO raid_member_snapshots
                        (raid_snapshot_id, name, path, slot, state)
                    VALUES (?,?,?,?,?)
                    """,
                    [(snap_id, m.name, m.path, m.slot, m.state) for m in raid.members],
                )

            for err in raid.errors:
                conn.execute(
                    "INSERT INTO probe_errors (collected_at, source, message, raid_name) VALUES (?,?,?,?)",
                    (ts, err.source, err.message, raid.name),
                )

        # ── Top-level errors ──────────────────────────────────────────────
        for err in overview.errors:
            conn.execute(
                "INSERT INTO probe_errors (collected_at, source, message) VALUES (?,?,?)",
                (ts, err.source, err.message),
            )



# ─────────────────────────────────────────────────────────────────────────────
# Database — pruning
# ─────────────────────────────────────────────────────────────────────────────

_PRUNE_CUTOFF_DAYS = 90  # 3 months


def prune_old_snapshots() -> None:
    """
    Delete all rows older than _PRUNE_CUTOFF_DAYS (90 days) from every
    time-series table and run VACUUM to reclaim disk space.

    Child rows (smart_attributes, nvme_health_snapshots, raid_member_snapshots)
    are removed automatically via ON DELETE CASCADE, so only the parent
    snapshot tables need to be targeted directly.

    Called by the scheduler once per day. Safe to call manually at any time.
    Never raises — errors are logged and swallowed so the scheduler stays alive.
    """
    cutoff = time.time() - (_PRUNE_CUTOFF_DAYS * 86400)

    try:
        with _db_conn() as conn:
            disk_del = conn.execute(
                "DELETE FROM disk_snapshots WHERE collected_at < ?", (cutoff,)
            ).rowcount
            raid_del = conn.execute(
                "DELETE FROM raid_snapshots WHERE collected_at < ?", (cutoff,)
            ).rowcount
            err_del = conn.execute(
                "DELETE FROM probe_errors WHERE collected_at < ?", (cutoff,)
            ).rowcount

        # VACUUM must run outside a transaction
        import sqlite3 as _sq
        with _sq.connect(load_config().db_path) as vconn:
            vconn.execute("PRAGMA journal_mode=WAL")
            vconn.execute("VACUUM")

        log.info(
            "Pruned rows older than %d days: %d disk snapshot(s), "
            "%d RAID snapshot(s), %d probe error(s). Database vacuumed.",
            _PRUNE_CUTOFF_DAYS, disk_del, raid_del, err_del,
        )
    except Exception:
        log.exception("prune_old_snapshots failed")

# ─────────────────────────────────────────────────────────────────────────────
# Database — query helpers
# ─────────────────────────────────────────────────────────────────────────────

def query_disk_history(
    name: str,
    limit: int = 100,
    since: float | None = None,
) -> DiskHistory | None:
    """
    Return time-series snapshots and a computed trend for *name*.

    Trend deltas compare the oldest to the newest snapshot in the result window.
    A positive reallocated_sectors_delta means the drive actively reallocated
    sectors during the window — a strong early-failure signal.
    """
    with _db_conn() as conn:
        # Identity info + total count from most recent snapshot
        meta = conn.execute(
            """
            SELECT name, model, serial, disk_type,
                   (SELECT COUNT(*) FROM disk_snapshots WHERE name = ?) AS total_snapshots
            FROM disk_snapshots
            WHERE name = ?
            ORDER BY collected_at DESC
            LIMIT 1
            """,
            (name, name),
        ).fetchone()

        if meta is None:
            return None

        where_since = "AND ds.collected_at >= ?" if since else ""
        params: list[Any] = [name]
        if since:
            params.append(since)
        params.append(limit)

        rows = conn.execute(
            f"""
            SELECT
                ds.id                AS snapshot_id,
                ds.collected_at,
                ds.smart_passed,
                ds.temperature_c,
                ds.power_on_hours,
                ds.reallocated_sectors,
                ds.pending_sectors,
                ds.uncorrectable_errors,
                nh.percentage_used   AS nvme_percentage_used,
                nh.media_errors      AS nvme_media_errors,
                nh.available_spare   AS nvme_available_spare,
                nh.critical_warning  AS nvme_critical_warning
            FROM disk_snapshots ds
            LEFT JOIN nvme_health_snapshots nh ON nh.snapshot_id = ds.id
            WHERE ds.name = ? {where_since}
            ORDER BY ds.collected_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    snapshots = [
        DiskSnapshot(
            snapshot_id=r["snapshot_id"],
            collected_at=r["collected_at"],
            smart_passed=None if r["smart_passed"] is None else bool(r["smart_passed"]),
            temperature_c=r["temperature_c"],
            power_on_hours=r["power_on_hours"],
            reallocated_sectors=r["reallocated_sectors"],
            pending_sectors=r["pending_sectors"],
            uncorrectable_errors=r["uncorrectable_errors"],
            nvme_percentage_used=r["nvme_percentage_used"],
            nvme_media_errors=r["nvme_media_errors"],
            nvme_available_spare=r["nvme_available_spare"],
            nvme_critical_warning=r["nvme_critical_warning"],
        )
        for r in rows
    ]

    if not snapshots:
        return None

    newest, oldest = snapshots[0], snapshots[-1]

    def _delta(new_val: int | None, old_val: int | None) -> int | None:
        return (new_val - old_val) if new_val is not None and old_val is not None else None

    temps = [s.temperature_c for s in snapshots if s.temperature_c is not None]

    trend = DiskTrend(
        reallocated_sectors_delta=_delta(newest.reallocated_sectors, oldest.reallocated_sectors),
        pending_sectors_delta=_delta(newest.pending_sectors, oldest.pending_sectors),
        uncorrectable_errors_delta=_delta(newest.uncorrectable_errors, oldest.uncorrectable_errors),
        nvme_wear_delta=_delta(newest.nvme_percentage_used, oldest.nvme_percentage_used),
        nvme_media_errors_delta=_delta(newest.nvme_media_errors, oldest.nvme_media_errors),
        temperature_max=max(temps) if temps else None,
        temperature_min=min(temps) if temps else None,
    )

    return DiskHistory(
        name=meta["name"],
        model=meta["model"],
        serial=meta["serial"],
        disk_type=meta["disk_type"],
        total_snapshots=meta["total_snapshots"],
        snapshots=snapshots,
        trend=trend,
    )


def query_raid_history(name: str, limit: int = 100) -> RaidHistory | None:
    with _db_conn() as conn:
        meta = conn.execute(
            """
            SELECT name, level,
                   (SELECT COUNT(*) FROM raid_snapshots WHERE name = ?) AS total
            FROM raid_snapshots
            WHERE name = ?
            ORDER BY collected_at DESC
            LIMIT 1
            """,
            (name, name),
        ).fetchone()

        if meta is None or meta["total"] == 0:
            return None

        rows = conn.execute(
            """
            SELECT id AS snapshot_id, collected_at, state,
                   active_devices, failed_devices, spare_devices, resync_percent
            FROM raid_snapshots
            WHERE name = ?
            ORDER BY collected_at DESC
            LIMIT ?
            """,
            (name, limit),
        ).fetchall()

        times_degraded = conn.execute(
            "SELECT COUNT(*) FROM raid_snapshots WHERE name = ? AND state != 'clean'",
            (name,),
        ).fetchone()[0]

    return RaidHistory(
        name=meta["name"],
        level=meta["level"],
        total_snapshots=meta["total"],
        snapshots=[
            RaidSnapshot(
                snapshot_id=r["snapshot_id"],
                collected_at=r["collected_at"],
                state=r["state"],
                active_devices=r["active_devices"],
                failed_devices=r["failed_devices"],
                spare_devices=r["spare_devices"],
                resync_percent=r["resync_percent"],
            )
            for r in rows
        ],
        times_degraded=times_degraded,
    )


def query_known_disks() -> list[str]:
    with _db_conn() as conn:
        return [r["name"] for r in conn.execute(
            "SELECT DISTINCT name FROM disk_snapshots ORDER BY name"
        ).fetchall()]


def query_known_raids() -> list[str]:
    with _db_conn() as conn:
        return [r["name"] for r in conn.execute(
            "SELECT DISTINCT name FROM raid_snapshots ORDER BY name"
        ).fetchall()]


def generate_alerts() -> AlertsSummary:
    """
    Build alerts from the most recent DB snapshot per device.

    Disk rules (CRITICAL → WARNING → INFO):
      smart_passed = False          → CRITICAL
      uncorrectable_errors > 0      → CRITICAL
      temperature ≥ critical_c      → CRITICAL
      nvme critical_warning != 0    → CRITICAL
      nvme wear ≥ critical_pct      → CRITICAL
      reallocated sectors increasing → CRITICAL
      reallocated sectors > 0       → WARNING
      pending sectors > 0           → WARNING
      temperature ≥ warning_c       → WARNING
      nvme wear ≥ warning_pct       → WARNING
      nvme media_errors > 0         → WARNING
      nvme spare below threshold    → INFO

    RAID rules:
      state == degraded / failed    → CRITICAL
      failed_devices > 0            → CRITICAL
      state == resyncing/recovering → WARNING
      no spare device (redundant)   → WARNING
    """
    cfg = load_config()
    alerts: list[Alert] = []
    now = time.time()

    with _db_conn() as conn:
        # Most recent snapshot per disk + NVMe health joined in
        disk_rows = conn.execute("""
            SELECT ds.*, nh.percentage_used AS nvme_pct,
                   nh.media_errors AS nvme_me,
                   nh.available_spare AS nvme_spare,
                   nh.available_spare_threshold AS nvme_spare_thresh,
                   nh.critical_warning AS nvme_cw
            FROM disk_snapshots ds
            LEFT JOIN nvme_health_snapshots nh ON nh.snapshot_id = ds.id
            WHERE ds.id IN (
                SELECT MAX(id) FROM disk_snapshots GROUP BY name
            )
        """).fetchall()

        # Second-most-recent snapshot per disk for trend comparison
        prev_rows = conn.execute("""
            SELECT name, reallocated_sectors, pending_sectors, uncorrectable_errors
            FROM disk_snapshots
            WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (PARTITION BY name ORDER BY collected_at DESC) AS rn
                    FROM disk_snapshots
                ) WHERE rn = 2
            )
        """).fetchall()
        prev_by_name = {r["name"]: r for r in prev_rows}

        # Most recent snapshot per RAID array
        raid_rows = conn.execute("""
            SELECT * FROM raid_snapshots
            WHERE id IN (
                SELECT MAX(id) FROM raid_snapshots GROUP BY name
            )
        """).fetchall()

    def add(level: AlertLevel, device: str, dtype: str, msg: str, value: Any = None) -> None:
        alerts.append(Alert(
            level=level, device=device, device_type=dtype,
            message=msg, value=value, detected_at=now,
        ))

    # ── Disk alerts ───────────────────────────────────────────────────────
    for r in disk_rows:
        name = r["name"]
        prev = prev_by_name.get(name)

        if r["smart_passed"] == 0:
            add(AlertLevel.CRITICAL, name, "disk", "SMART health assessment FAILED")

        ue = r["uncorrectable_errors"]
        if ue is not None and ue > 0:
            add(AlertLevel.CRITICAL, name, "disk",
                "Uncorrectable errors detected (attr 198)", ue)

        temp = r["temperature_c"]
        if temp is not None:
            if temp >= cfg.alert_temp_critical_c:
                add(AlertLevel.CRITICAL, name, "disk",
                    f"Temperature {temp}°C ≥ critical threshold ({cfg.alert_temp_critical_c}°C)", temp)
            elif temp >= cfg.alert_temp_warning_c:
                add(AlertLevel.WARNING, name, "disk",
                    f"Temperature {temp}°C ≥ warning threshold ({cfg.alert_temp_warning_c}°C)", temp)

        nvme_cw = r["nvme_cw"]
        if nvme_cw is not None and nvme_cw != 0:
            add(AlertLevel.CRITICAL, name, "disk",
                "NVMe critical_warning bitmask is set", hex(nvme_cw))

        nvme_pct = r["nvme_pct"]
        if nvme_pct is not None:
            if nvme_pct >= cfg.alert_nvme_wear_critical_pct:
                add(AlertLevel.CRITICAL, name, "disk",
                    f"NVMe wear {nvme_pct}% ≥ critical ({cfg.alert_nvme_wear_critical_pct}%)", nvme_pct)
            elif nvme_pct >= cfg.alert_nvme_wear_warning_pct:
                add(AlertLevel.WARNING, name, "disk",
                    f"NVMe wear {nvme_pct}% ≥ warning ({cfg.alert_nvme_wear_warning_pct}%)", nvme_pct)

        nvme_me = r["nvme_me"]
        if nvme_me is not None and nvme_me > 0:
            add(AlertLevel.WARNING, name, "disk", "NVMe media errors detected", nvme_me)

        nvme_spare = r["nvme_spare"]
        nvme_thresh = r["nvme_spare_thresh"]
        if nvme_spare is not None and nvme_thresh is not None and nvme_spare < nvme_thresh:
            add(AlertLevel.INFO, name, "disk",
                f"NVMe available spare ({nvme_spare}%) below threshold ({nvme_thresh}%)", nvme_spare)

        realloc = r["reallocated_sectors"]
        if realloc is not None and realloc > 0:
            if prev and prev["reallocated_sectors"] is not None:
                delta = realloc - prev["reallocated_sectors"]
                if delta > 0:
                    add(AlertLevel.CRITICAL, name, "disk",
                        f"Reallocated sectors INCREASING (+{delta} since last snapshot)", realloc)
                else:
                    add(AlertLevel.WARNING, name, "disk",
                        f"Reallocated sectors present (stable at {realloc})", realloc)
            else:
                add(AlertLevel.WARNING, name, "disk", "Reallocated sectors present", realloc)

        pending = r["pending_sectors"]
        if pending is not None and pending > 0:
            add(AlertLevel.WARNING, name, "disk", "Pending (unstable) sectors detected", pending)

    # ── RAID alerts ───────────────────────────────────────────────────────
    for r in raid_rows:
        name = r["name"]
        state = r["state"]
        failed = r["failed_devices"]
        spare = r["spare_devices"]
        level_str = r["level"] or ""

        if state in ("degraded", "failed"):
            add(AlertLevel.CRITICAL, name, "raid", f"Array is {state}", state)
        elif state in ("resyncing", "recovering"):
            pct = r["resync_percent"]
            msg = f"Array is {state}"
            if pct is not None:
                msg += f" ({pct:.1f}% complete)"
            add(AlertLevel.WARNING, name, "raid", msg, pct)

        if failed and failed > 0:
            add(AlertLevel.CRITICAL, name, "raid",
                f"{failed} member device(s) have failed", failed)

        # Warn if no spare on a redundant array (raid0 and linear cannot rebuild)
        total = r["total_devices"] or 0
        if (spare is not None and spare == 0 and total > 1
                and "raid0" not in level_str and "linear" not in level_str):
            add(AlertLevel.WARNING, name, "raid",
                "No hot spare — array cannot auto-rebuild if a member fails")

    criticals = sum(1 for a in alerts if a.level == AlertLevel.CRITICAL)
    warnings = sum(1 for a in alerts if a.level == AlertLevel.WARNING)
    return AlertsSummary(alerts=alerts, generated_at=now, criticals=criticals, warnings=warnings)


# ─────────────────────────────────────────────────────────────────────────────
# Probe pipeline
# ─────────────────────────────────────────────────────────────────────────────

_ATA_ATTRS_OF_INTEREST: dict[int, str] = {
    5: "reallocated_sectors",
    197: "pending_sectors",
    198: "uncorrectable_errors",
}


def _enumerate_disks() -> tuple[list[dict[str, Any]], list[ProbeError]]:
    errors: list[ProbeError] = []
    stdout, err = _cmd(["lsblk", "--json", "--bytes",
                        "--output", "NAME,TYPE,SIZE,ROTA,TRAN,MOUNTPOINT"])
    if err:
        errors.append(ProbeError(source="lsblk", message=err))
        return [], errors
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        errors.append(ProbeError(source="lsblk", message=f"JSON parse: {exc}"))
        return [], errors
    return [d for d in data.get("blockdevices", []) if d.get("type") == "disk"], errors


def _parse_mdstat() -> tuple[dict[str, RaidInfo], dict[str, str]]:
    stdout, err = _cmd(["cat", "/proc/mdstat"])
    raids: dict[str, RaidInfo] = {}
    raid_members: dict[str, str] = {}
    if err:
        log.error("Cannot read /proc/mdstat: %s", err)
        return raids, raid_members

    current: str | None = None
    for line in stdout.splitlines():
        header = re.match(r"^(md\d+)\s*:\s*(.+)", line)
        if header:
            current = header.group(1)
            info_str = header.group(2)
            level_m = re.search(r"(?:^|\s)(raid\d+|linear|multipath|faulty)(?:\s|$)", info_str)
            state = ("inactive" if "inactive" in info_str
                     else "clean" if "active" in info_str else "unknown")
            members: list[RaidMember] = []
            for m in re.finditer(r"([\w]+)\[(\d+)\](\(F\)|\(S\))?", info_str):
                dn, slot, flag = m.group(1), int(m.group(2)), m.group(3) or ""
                ms = "faulty" if flag == "(F)" else "spare" if flag == "(S)" else "active sync"
                members.append(RaidMember(name=dn, path=f"/dev/{dn}", slot=slot, state=ms))
                raid_members[dn] = current
            raids[current] = RaidInfo(
                name=current, path=f"/dev/{current}",
                level=level_m.group(1) if level_m else None,
                state=state, members=members,
            )
            continue
        if current is None or not line.strip():
            continue
        raid = raids[current]
        if m := re.search(r"\[(\d+)/(\d+)\]", line):
            raid.total_devices = int(m.group(1))
            raid.active_devices = int(m.group(2))
            if raid.active_devices < raid.total_devices:
                raid.state = "degraded"
        if m := re.search(r"(\d+) blocks", line):
            raid.size_bytes = int(m.group(1)) * 1024
        if m := re.search(r"(\d+)k chunk", line, re.IGNORECASE):
            raid.chunk_size_kb = int(m.group(1))
        if m := re.search(r"(?:resync|recovery|check)\s*=\s*([\d.]+)%", line):
            raid.resync_percent = float(m.group(1))
            raid.state = "resyncing"
            if sm := re.search(r"speed=(\d+)K/sec", line):
                raid.resync_speed_mib = round(int(sm.group(1)) / 1024, 2)
    return raids, raid_members


def _enrich_raids_mdadm(raids: dict[str, RaidInfo]) -> None:
    for name, raid in raids.items():
        stdout, err = _cmd(["sudo", "mdadm", "--detail", f"/dev/{name}"])
        if err:
            raid.errors.append(ProbeError(source=f"mdadm:{name}", message=err))
            continue
        in_table = False
        for line in stdout.splitlines():
            s = line.strip()
            if m := re.match(r"State\s*:\s*(.+)", s):
                raw = m.group(1).lower()
                raid.state = ("resyncing" if "resyncing" in raw or "recovering" in raw
                              else "degraded" if "degraded" in raw
                              else "clean" if "clean" in raw or "active" in raw
                              else "inactive" if "inactive" in raw
                              else raw.split(",")[0].strip())
            elif m := re.match(r"Raid Level\s*:\s*(.+)", s):
                raid.level = m.group(1).strip()
            elif m := re.match(r"Array Size\s*:\s*(\d+)", s):
                raid.size_bytes = int(m.group(1)) * 1024
            elif m := re.match(r"Raid Devices\s*:\s*(\d+)", s):
                raid.total_devices = int(m.group(1))
            elif m := re.match(r"Active Devices\s*:\s*(\d+)", s):
                raid.active_devices = int(m.group(1))
            elif m := re.match(r"Working Devices\s*:\s*(\d+)", s):
                raid.working_devices = int(m.group(1))
            elif m := re.match(r"Failed Devices\s*:\s*(\d+)", s):
                raid.failed_devices = int(m.group(1))
            elif m := re.match(r"Spare Devices\s*:\s*(\d+)", s):
                raid.spare_devices = int(m.group(1))
            elif m := re.match(r"Chunk Size\s*:\s*(\d+)K", s):
                raid.chunk_size_kb = int(m.group(1))
            elif "RaidDevice" in s and "State" in s:
                in_table = True
            elif in_table and s:
                parts = s.split()
                dev_path = parts[-1] if parts[-1].startswith("/dev/") else None
                if not dev_path:
                    continue
                dev_name = dev_path.removeprefix("/dev/")
                slot_str = parts[3]
                slot = int(slot_str) if slot_str.isdigit() else None
                mstate = " ".join(parts[4:-1])
                existing = next((r for r in raid.members if r.name == dev_name), None)
                if existing:
                    existing.state = mstate
                    if slot is not None:
                        existing.slot = slot
                else:
                    raid.members.append(
                        RaidMember(name=dev_name, path=dev_path, slot=slot, state=mstate)
                    )


def _parse_smartctl_json(
    data: dict[str, Any],
) -> tuple[SmartInfo, str | None, str | None, str | None, int | None]:
    smart = SmartInfo()
    if "passed" in data.get("smart_status", {}):
        smart.passed = bool(data["smart_status"]["passed"])
    smart.temperature_c = data.get("temperature", {}).get("current")
    smart.power_on_hours = data.get("power_on_time", {}).get("hours")

    nvme_log = data.get("nvme_smart_health_information_log")
    if nvme_log:
        smart.nvme = NvmeHealth(
            percentage_used=nvme_log.get("percentage_used"),
            media_errors=nvme_log.get("media_errors"),
            unsafe_shutdowns=nvme_log.get("unsafe_shutdowns"),
            power_cycles=nvme_log.get("power_cycles"),
            available_spare=nvme_log.get("available_spare"),
            available_spare_threshold=nvme_log.get("available_spare_threshold"),
            critical_warning=nvme_log.get("critical_warning"),
        )

    parsed: list[SmartAttribute] = []
    for entry in data.get("ata_smart_attributes", {}).get("table", []):
        attr_id = entry.get("id", 0)
        raw_val = entry.get("raw", {}).get("value", 0)
        failed_str = entry.get("when_failed", "")
        parsed.append(SmartAttribute(
            id=attr_id, name=entry.get("name", f"attr_{attr_id}"),
            value=entry.get("value", 0), worst=entry.get("worst", 0),
            threshold=entry.get("thresh", 0), raw_value=raw_val,
            failed=bool(failed_str and failed_str not in ("-", "")),
        ))
        field = _ATA_ATTRS_OF_INTEREST.get(attr_id)
        if field == "reallocated_sectors":
            smart.reallocated_sectors = raw_val
        elif field == "pending_sectors":
            smart.pending_sectors = raw_val
        elif field == "uncorrectable_errors":
            smart.uncorrectable_errors = raw_val
    smart.attributes = parsed

    model = data.get("model_name") or data.get("model_family") or None
    serial = data.get("serial_number") or None
    firmware = data.get("firmware_version") or None
    rot = data.get("rotation_rate")
    rpm: int | None = int(rot) if isinstance(rot, (int, float)) and rot > 0 else None
    return smart, model, serial, firmware, rpm


def probe_smart(
    path: str,
) -> tuple[SmartInfo, str | None, str | None, str | None, int | None, list[ProbeError]]:
    errors: list[ProbeError] = []
    stdout, err = _cmd(["sudo", "smartctl", "-a", "--json=c", path])
    if err:
        errors.append(ProbeError(source=f"smartctl:{path}", message=err))
        return SmartInfo(), None, None, None, None, errors
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        errors.append(ProbeError(source=f"smartctl:{path}", message=f"JSON parse: {exc}"))
        return SmartInfo(), None, None, None, None, errors
    smart, model, serial, firmware, rpm = _parse_smartctl_json(data)
    if smart.passed is None and not smart.attributes and smart.nvme is None:
        errors.append(ProbeError(
            source=f"smartctl:{path}",
            message="No SMART data — device may not support SMART or interface blocks passthrough",
        ))
    return smart, model, serial, firmware, rpm, errors


def _build_disks(
    lsblk_devices: list[dict[str, Any]],
    raid_members: dict[str, str],
) -> tuple[dict[str, DiskInfo], list[ProbeError]]:
    disks: dict[str, DiskInfo] = {}
    global_errors: list[ProbeError] = []
    for dev in lsblk_devices:
        name: str = dev.get("name", "")
        if not name:
            continue
        path = f"/dev/{name}"
        tran = (dev.get("tran") or "").lower()
        size_str = str(dev.get("size") or "")
        size_bytes: int | None = int(size_str) if size_str.isdigit() else None
        disk_type = (
            "nvme" if ("nvme" in name or tran == "nvme")
            else "ata" if tran in ("sata", "ata")
            else "scsi" if tran == "sas"
            else "unknown"
        )
        raid_member_of: str | None = None
        for member_name, md_name in raid_members.items():
            if re.sub(r"\d+$", "", member_name) == name or member_name == name:
                raid_member_of = md_name
                break
        smart, model, serial, firmware, rpm, smart_errors = probe_smart(path)
        disks[name] = DiskInfo(
            name=name, path=path, disk_type=disk_type, transport=tran or None,
            size_bytes=size_bytes, model=model, serial=serial, firmware=firmware,
            rpm=rpm, raid_member_of=raid_member_of, smart=smart, errors=smart_errors,
        )
        global_errors.extend(smart_errors)
    return disks, global_errors


# ─────────────────────────────────────────────────────────────────────────────
# In-memory cache (short TTL for live endpoints between scheduled runs)
# ─────────────────────────────────────────────────────────────────────────────

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"data": None, "ts": 0.0}


def collect() -> SystemOverview:
    cfg = load_config()
    now = time.monotonic()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["ts"]) < cfg.cache_ttl:
            return _cache["data"]

    lsblk_devices, lsblk_errors = _enumerate_disks()
    raids, raid_members = _parse_mdstat()
    _enrich_raids_mdadm(raids)
    disks, _ = _build_disks(lsblk_devices, raid_members)
    overview = SystemOverview(
        disks=disks, raids=raids, collected_at=time.time(), errors=lsblk_errors,
    )
    with _cache_lock:
        _cache["data"] = overview
        _cache["ts"] = time.monotonic()
    return overview


def collect_and_save() -> None:
    """
    Called by the scheduler. Forces a fresh probe (ignores cache), saves to the
    database, then updates the cache so subsequent API calls are served instantly.
    """
    log.info("Scheduled collection starting …")
    try:
        lsblk_devices, lsblk_errors = _enumerate_disks()
        raids, raid_members = _parse_mdstat()
        _enrich_raids_mdadm(raids)
        disks, _ = _build_disks(lsblk_devices, raid_members)
        overview = SystemOverview(
            disks=disks, raids=raids, collected_at=time.time(), errors=lsblk_errors,
        )
        save_snapshot(overview)
        with _cache_lock:
            _cache["data"] = overview
            _cache["ts"] = time.monotonic()
        log.info("Collection saved: %d disk(s), %d RAID array(s)", len(disks), len(raids))
    except Exception:
        log.exception("Scheduled collection failed")


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI application
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Disk & RAID Monitor",
    description="""
Monitor physical disks and Linux software RAID arrays with historical trend
analysis and threshold-based alerting.

## Live endpoints
Probe devices on demand (served from cache if fresh).

## History endpoints
Query the SQLite database for time-series snapshots and computed trend deltas.
Use the `since` parameter (Unix timestamp) to narrow the window.

## Alerts
`GET /alerts` reads only the database — it never probes devices — so it is
always fast. It evaluates threshold rules and cross-snapshot trend detection
(e.g. reallocated sectors increasing between the last two snapshots).

## Scheduling
The scheduler runs `collect_and_save()` every `SCHEDULE_INTERVAL_MINUTES`
minutes (default 15), so the history grows automatically.
    """,
    version="4.0.0",
)

_AUTH = [Security(require_token)]

# ── Live ──────────────────────────────────────────────────────────────────────

@app.get("/system", response_model=SystemOverview, summary="Full system snapshot",
         tags=["Live"], dependencies=_AUTH)
def system_overview() -> SystemOverview:
    """All disk and RAID data in one call (from cache if fresh)."""
    return collect()

@app.get("/disks", response_model=dict[str, DiskInfo], summary="All disks",
         tags=["Live"], dependencies=_AUTH)
def list_disks() -> dict[str, DiskInfo]:
    return collect().disks

@app.get("/disks/{name}", response_model=DiskInfo, summary="Single disk",
         tags=["Live"], dependencies=_AUTH)
def get_disk(name: str) -> DiskInfo:
    disks = collect().disks
    if name not in disks:
        raise HTTPException(404, f"Disk '{name}' not found. Known: {sorted(disks)}")
    return disks[name]

@app.get("/raids", response_model=dict[str, RaidInfo], summary="All RAID arrays",
         tags=["Live"], dependencies=_AUTH)
def list_raids() -> dict[str, RaidInfo]:
    return collect().raids

@app.get("/raids/{name}", response_model=RaidInfo, summary="Single RAID array",
         tags=["Live"], dependencies=_AUTH)
def get_raid(name: str) -> RaidInfo:
    raids = collect().raids
    if name not in raids:
        raise HTTPException(404, f"RAID '{name}' not found. Known: {sorted(raids)}")
    return raids[name]

# ── History ───────────────────────────────────────────────────────────────────

@app.get("/history/disks", response_model=list[str], summary="All tracked disks",
         tags=["History"], dependencies=_AUTH)
def history_disk_list() -> list[str]:
    """Every disk name that has at least one snapshot in the database."""
    return query_known_disks()

@app.get("/history/disks/{name}", response_model=DiskHistory,
         summary="Disk history + trend", tags=["History"], dependencies=_AUTH)
def disk_history(
    name: str,
    limit: int = Query(100, ge=1, le=5000, description="Max snapshots to return"),
    since: float | None = Query(None, description="Only return snapshots after this Unix timestamp"),
) -> DiskHistory:
    """
    Time-series SMART snapshots for one disk with computed trend deltas.

    A positive `reallocated_sectors_delta` means the drive is actively failing.
    `nvme_wear_delta` shows how fast TBW is being consumed.
    `temperature_max` helps spot thermal events.
    """
    result = query_disk_history(name, limit=limit, since=since)
    if result is None:
        raise HTTPException(404, f"No history for '{name}'. Known: {query_known_disks()}")
    return result

@app.get("/history/raids", response_model=list[str], summary="All tracked RAID arrays",
         tags=["History"], dependencies=_AUTH)
def history_raid_list() -> list[str]:
    return query_known_raids()

@app.get("/history/raids/{name}", response_model=RaidHistory,
         summary="RAID array history", tags=["History"], dependencies=_AUTH)
def raid_history(
    name: str,
    limit: int = Query(100, ge=1, le=5000),
) -> RaidHistory:
    """
    State snapshots for one RAID array. `times_degraded` counts non-clean
    snapshots across the full history (not just the requested window).
    """
    result = query_raid_history(name, limit=limit)
    if result is None:
        raise HTTPException(404, f"No history for '{name}'. Known: {query_known_raids()}")
    return result

# ── Alerts ────────────────────────────────────────────────────────────────────

@app.get("/alerts", response_model=AlertsSummary, summary="Active alerts",
         tags=["Alerts"], dependencies=_AUTH)
def alerts() -> AlertsSummary:
    """
    Threshold and trend alerts from the most recent DB snapshot per device.
    Never triggers a live probe — always fast to call.
    """
    return generate_alerts()

# ── Meta ──────────────────────────────────────────────────────────────────────

@app.get("/health", summary="Liveness probe", tags=["Meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}

# ─────────────────────────────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup() -> None:
    init_db()

    cfg = load_config()
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        collect_and_save,
        trigger="interval",
        minutes=cfg.schedule_interval_minutes,
        id="collect_and_save",
        max_instances=1,   # never overlap if a run runs long
        coalesce=True,     # if multiple fires were missed, run once
    )
    scheduler.add_job(
        prune_old_snapshots,
        trigger="interval",
        hours=24,
        id="prune_old_snapshots",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    # Immediate first collection so the DB has data from the first second
    threading.Thread(target=collect_and_save, daemon=True, name="initial-collect").start()

    log.info(
        "Scheduler started — collection every %d min, pruning every 24 h (cutoff: %d days). DB: %s",
        cfg.schedule_interval_minutes, _PRUNE_CUTOFF_DAYS, cfg.db_path,
    )

# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cfg = load_config()
    ssl_kwargs: dict[str, Any] = {}
    if cfg.use_https:
        cert, key = Path(cfg.certificate_path), Path(cfg.key_path)
        if cert.exists() and key.exists():
            ssl_kwargs = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
        else:
            log.warning("TLS cert/key not found — falling back to plain HTTP")
    log.info("Starting on %s:%d  TLS=%s", cfg.host, cfg.port, bool(ssl_kwargs))
    uvicorn.run("disk_monitor:app", host=cfg.host, port=cfg.port,
                log_level="info", **ssl_kwargs)
