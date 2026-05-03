# disk-monitor

A self-contained REST API for monitoring physical disk health and Linux software RAID arrays, with a matching Home Assistant custom integration that turns every disk and array into native HA entities.

- Single Python process — no database server, no message queue
- SQLite history with automatic 90-day pruning
- Threshold and trend-based alerts (reallocated sectors increasing, NVMe wear, temperature, RAID degraded)
- Home Assistant integration with sensors, binary sensors, and config-flow UI setup

---

## Table of contents

- [How it works](#how-it-works)
- [Repository layout](#repository-layout)
- [API server — requirements](#api-server--requirements)
- [API server — installation](#api-server--installation)
- [API server — configuration](#api-server--configuration)
- [API server — running](#api-server--running)
- [API reference](#api-reference)
- [Database](#database)
- [Home Assistant integration](#home-assistant-integration)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## How it works

Every 15 minutes (configurable) the scheduler probes all devices and writes a snapshot to a local SQLite database. The API serves live data from an in-memory cache and historical trend data from the database. The `/alerts` endpoint reads only the database and never triggers a hardware probe, so it is always fast to poll.

The probe pipeline runs in four steps:

| Step | Tool | What it provides |
|------|------|-----------------|
| 1 | `lsblk --json` | Physical disk enumeration — no partitions, LVM volumes, or loop devices |
| 2 | `/proc/mdstat` | RAID array list, member mapping, live resync progress |
| 3 | `mdadm --detail` | Authoritative array state, device counts, per-member states |
| 4 | `smartctl -a --json` | Health pass/fail, all SMART attributes, temperature, NVMe health log, identity |

---

## Repository layout

```
disk_monitor.py          API server
disk_monitor.conf        Configuration (copy from disk_monitor.conf.example)
custom_components/
  disk_monitor/
    __init__.py          Integration entry point
    manifest.json        HA integration metadata
    const.py             Shared constants
    coordinator.py       DataUpdateCoordinator — polls /system and /alerts
    config_flow.py       UI setup wizard (tests connection before saving)
    sensor.py            Numeric sensors (temperature, wear, RAID state, …)
    binary_sensor.py     Fault sensors (SMART failed, RAID degraded, …)
    strings.json         UI strings
    translations/
      en.json            English translations
```

---

## API server — requirements

**Operating system:** Linux (kernel 3.9+). Tested on Debian, Ubuntu, and Arch.

**System packages:**

```bash
# Debian / Ubuntu
sudo apt install smartmontools mdadm util-linux

# Arch
sudo pacman -S smartmontools mdadm util-linux
```

**Python:** 3.11 or newer.

> Python 3.11+ is required for the `X | Y` union type syntax used throughout the codebase. Check your version with `python3 --version`.

---

## API server — installation

### 1. Clone the repository

```bash
git clone https://github.com/your-username/disk-monitor.git
cd disk-monitor
```

### 2. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Python dependencies

```bash
pip install fastapi "uvicorn[standard]" pydantic apscheduler
```

### 4. Allow smartctl and mdadm to run without a password

The probe pipeline calls `sudo smartctl` and `sudo mdadm`. Add a sudoers rule so the service account can run these two commands without a password prompt:

```bash
sudo visudo -f /etc/sudoers.d/disk-monitor
```

Paste the following, replacing `YOUR_USER` with the user that will run the service:

```
YOUR_USER ALL=(ALL) NOPASSWD: /usr/sbin/smartctl, /usr/sbin/mdadm
```

Save and verify:

```bash
sudo -l -U YOUR_USER
```

You should see `(ALL) NOPASSWD: /usr/sbin/smartctl, /usr/sbin/mdadm` in the output.

---

## API server — configuration

Copy the example config and edit it:

```bash
cp disk_monitor.conf.example disk_monitor.conf
```

`disk_monitor.conf` must live in the same directory as `disk_monitor.py`. All keys are in the `[DEFAULT]` section:

```ini
[DEFAULT]
HOST             = 0.0.0.0
PORT             = 8000
USE_HTTPS        = false
CERTIFICATE_PATH = /etc/ssl/certs/disk_monitor.crt
KEY_PATH         = /etc/ssl/private/disk_monitor.key

; Leave TOKEN empty to disable authentication entirely
TOKEN            =

; SQLite database path — relative to the script or absolute
DB_PATH          = disk_monitor.db

; How often the scheduler probes all devices (minutes)
SCHEDULE_INTERVAL_MINUTES = 15

; Alert thresholds
ALERT_TEMP_WARNING_C         = 55
ALERT_TEMP_CRITICAL_C        = 65
ALERT_NVME_WEAR_WARNING_PCT  = 80
ALERT_NVME_WEAR_CRITICAL_PCT = 95
```

### Setting up an API key

Generate a secure random key:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Paste the output into `disk_monitor.conf`:

```ini
TOKEN = your-generated-key-here
```

Restart the service after any config change. The key is read once at startup and cached.

### Enabling HTTPS

Obtain a certificate and key (e.g. via Let's Encrypt / Certbot), then set:

```ini
USE_HTTPS        = true
CERTIFICATE_PATH = /etc/letsencrypt/live/your-domain/fullchain.pem
KEY_PATH         = /etc/letsencrypt/live/your-domain/privkey.pem
```

If the paths do not exist at startup the service falls back to plain HTTP and logs a warning.

---

## API server — running

### Directly

```bash
source .venv/bin/activate
python3 disk_monitor.py
```

Or via Uvicorn directly (useful during development with `--reload`):

```bash
uvicorn disk_monitor:app --host 0.0.0.0 --port 8000 --reload
```

### As a systemd service (recommended for production)

Create the unit file:

```bash
sudo nano /etc/systemd/system/disk-monitor.service
```

```ini
[Unit]
Description=Disk & RAID Monitor API
After=network.target
Wants=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/opt/disk-monitor
ExecStart=/opt/disk-monitor/.venv/bin/python3 disk_monitor.py
Restart=on-failure
RestartSec=10
; Give smartctl time to finish on slow disks before the process is killed
TimeoutStopSec=60

; Harden the service — remove lines that conflict with your setup
ProtectSystem=strict
ReadWritePaths=/opt/disk-monitor
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now disk-monitor
sudo systemctl status disk-monitor
```

Check logs:

```bash
journalctl -u disk-monitor -f
```

---

## API reference

All endpoints except `GET /health` require the `X-API-Key` header when `TOKEN` is set in the config.

### Authentication

```bash
# Preferred — key in header (never written to server logs or browser history)
curl -H "X-API-Key: your-key" http://localhost:8000/disks

# Fallback — query parameter (for browser testing only, avoid in production)
curl http://localhost:8000/disks?token=your-key
```

| Response code | Meaning |
|---------------|---------|
| `200` | Success |
| `401` | No key supplied and one is required |
| `403` | Key supplied but incorrect |
| `404` | Device or array not found |

### Interactive docs

When the service is running, full interactive documentation is available at:

- **Swagger UI** — `http://localhost:8000/docs`
- **ReDoc** — `http://localhost:8000/redoc`

Click **Authorize** in the Swagger UI, enter your API key once, and it is applied to all requests in the session.

---

### Live endpoints

These endpoints return data from the in-memory cache (refreshed every `CACHE_TTL` seconds, default 60). If the cache is stale a fresh probe is triggered.

---

`GET /system`

Full system snapshot — all disks and all RAID arrays in one response. Useful for dashboards that need everything at once.

---

`GET /disks`

All physical disks with SMART data, identity, and temperature. Keys are kernel device names.

```bash
curl -H "X-API-Key: your-key" http://localhost:8000/disks
```

---

`GET /disks/{name}`

Single disk by kernel device name.

```bash
curl -H "X-API-Key: your-key" http://localhost:8000/disks/sda
curl -H "X-API-Key: your-key" http://localhost:8000/disks/nvme0n1
```

---

`GET /raids`

All md RAID arrays with state, member list, device counts, and resync progress.

---

`GET /raids/{name}`

Single RAID array by name.

```bash
curl -H "X-API-Key: your-key" http://localhost:8000/raids/md0
```

---

### History endpoints

These endpoints read only the SQLite database. No hardware is probed, so they are always fast.

---

`GET /history/disks`

List of all disk names that have at least one snapshot in the database.

---

`GET /history/disks/{name}?limit=100&since=<unix_timestamp>`

Time-series SMART snapshots for one disk with computed trend deltas.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | `100` | Maximum snapshots to return (max 5000) |
| `since` | float | — | Unix timestamp — only return snapshots after this point in time |

The `trend` object compares the oldest to the newest snapshot in the window:

- `reallocated_sectors_delta > 0` — the drive is actively reallocating sectors right now. Replace it.
- `nvme_wear_delta` — percentage points of TBW consumed in the window.
- `temperature_max` / `temperature_min` — useful for spotting thermal events.

```bash
# Last 24 hours of snapshots for sda
curl -H "X-API-Key: your-key" \
  "http://localhost:8000/history/disks/sda?since=$(date -d '24 hours ago' +%s)"
```

---

`GET /history/raids`

List of all RAID array names that have at least one snapshot in the database.

---

`GET /history/raids/{name}?limit=100`

State snapshots for one RAID array. `times_degraded` counts non-clean snapshots across the full history (not just the requested window), giving a reliability summary.

---

### Alerts endpoint

`GET /alerts`

Threshold and trend alerts derived from the most recent snapshot per device. Never triggers a live probe — safe to poll frequently.

| Level | Conditions |
|-------|-----------|
| `critical` | SMART health assessment failed · uncorrectable errors detected · temperature ≥ critical threshold · NVMe `critical_warning` bitmask set · NVMe wear ≥ critical threshold · reallocated sectors increasing between last two snapshots · RAID state is degraded or failed |
| `warning` | Reallocated sectors present but stable · pending sectors · temperature ≥ warning threshold · NVMe wear ≥ warning threshold · NVMe media errors · RAID resyncing or recovering · no hot spare on a redundant array |
| `info` | NVMe available spare below threshold |

```bash
curl -H "X-API-Key: your-key" http://localhost:8000/alerts
```

---

### Meta

`GET /health`

Liveness probe. Returns `{"status": "ok"}`. No authentication required. Does not trigger data collection. Use this for load balancer health checks and the HA integration connection test.

---

## Database

The SQLite database (`disk_monitor.db` by default) is created automatically on first startup. It uses WAL mode so API read requests and the scheduler's write operations never block each other.

**Schema overview:**

| Table | Contents |
|-------|----------|
| `disk_snapshots` | One row per disk per collection run — identity and SMART headline values |
| `smart_attributes` | Full ATA attribute table linked to each disk snapshot via foreign key |
| `nvme_health_snapshots` | NVMe health log linked to each disk snapshot |
| `raid_snapshots` | One row per RAID array per collection run |
| `raid_member_snapshots` | Per-member state linked to each RAID snapshot |
| `probe_errors` | All non-fatal probe errors from every collection run |

**Automatic pruning:** rows older than 90 days are deleted automatically once per day by the built-in scheduler job. A `VACUUM` follows each prune to reclaim disk space. You can also prune manually:

```bash
sqlite3 disk_monitor.db "
  DELETE FROM disk_snapshots WHERE collected_at < strftime('%s','now','-90 days');
  DELETE FROM raid_snapshots  WHERE collected_at < strftime('%s','now','-90 days');
  DELETE FROM probe_errors    WHERE collected_at < strftime('%s','now','-90 days');
  VACUUM;
"
```

**Storage estimate:** roughly 50–150 KB per collection run. At the default 15-minute interval on a system with six disks that is about 5–15 MB per day, or 450 MB–1.35 GB per 90-day window before pruning kicks in.

---

## Home Assistant integration

The `custom_components/disk_monitor` folder is a native Home Assistant integration. It polls the disk-monitor API and exposes every disk and RAID array as HA devices with individual sensor and binary sensor entities.

### What you get

**Per physical disk:**

| Entity | Type | Description |
|--------|------|-------------|
| Temperature | Sensor (°C) | Current disk temperature |
| Power-on hours | Sensor (h) | Total hours powered on |
| Reallocated sectors | Sensor | ATA attribute 5 raw value |
| Pending sectors | Sensor | ATA attribute 197 raw value |
| Uncorrectable errors | Sensor | ATA attribute 198 raw value |
| NVMe wear | Sensor (%) | NVMe `percentage_used` — only on NVMe disks |
| NVMe available spare | Sensor (%) | Remaining spare capacity — only on NVMe disks |
| NVMe media errors | Sensor | Cumulative media error count — only on NVMe disks |
| SMART health | Binary sensor | `problem` when SMART health assessment fails |
| Reallocated sectors | Binary sensor | `problem` when reallocated sector count > 0 |
| Pending sectors | Binary sensor | `problem` when pending sector count > 0 |
| Uncorrectable errors | Binary sensor | `problem` when uncorrectable error count > 0 |

**Per RAID array:**

| Entity | Type | Description |
|--------|------|-------------|
| State | Sensor | `clean` · `degraded` · `resyncing` · `inactive` |
| Active devices | Sensor | Number of active member disks |
| Failed devices | Sensor | Number of failed member disks |
| Spare devices | Sensor | Number of hot spare disks |
| Resync progress | Sensor (%) | Resync / recovery completion percentage |
| Degraded | Binary sensor | `problem` when state is not `clean` |
| Failed devices | Binary sensor | `problem` when any member has failed |
| No hot spare | Binary sensor | `problem` when a redundant array has no spare |

**Global (one set per configured server):**

| Entity | Type | Description |
|--------|------|-------------|
| Critical alerts | Sensor | Count of active critical alerts |
| Warning alerts | Sensor | Count of active warning alerts |
| Has critical alerts | Binary sensor | `problem` when any critical alert exists |
| Has warning alerts | Binary sensor | `problem` when any warning alert exists |

### Installation

Copy the `custom_components/disk_monitor` folder into your Home Assistant configuration directory:

```bash
# If your HA config is at /config (common in HA OS / Docker)
cp -r custom_components/disk_monitor /config/custom_components/

# Or for a manual HA install
cp -r custom_components/disk_monitor ~/.homeassistant/custom_components/
```

Restart Home Assistant. The integration will appear in the integrations catalogue once HA has picked up the new files.

### Setup via UI

1. Go to **Settings → Devices & Services**.
2. Click **Add Integration** (bottom right).
3. Search for **Disk & RAID Monitor**.
4. Fill in the form:

| Field | Description |
|-------|-------------|
| Host or IP address | The machine running `disk_monitor.py` |
| Port | Default `8000` |
| API key | The value of `TOKEN` in `disk_monitor.conf` — leave empty if auth is disabled |
| Use HTTPS | Enable if the server is configured with TLS |
| Verify SSL certificate | Disable only if using a self-signed certificate |
| Poll interval (seconds) | How often HA fetches fresh data — default `60` |

HA calls `GET /health` with your API key before saving. If the connection or key is wrong you get an error in the form immediately.

5. Click **Submit**. HA creates one device per physical disk, one device per RAID array, and one service device for global alert sensors.

### Example automations

Send a notification when any disk has a critical problem:

```yaml
automation:
  - alias: "Disk monitor — critical alert"
    trigger:
      - platform: state
        entity_id: binary_sensor.disk_monitor_has_critical_alerts
        to: "on"
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "⚠️ Disk Monitor — Critical Alert"
          message: >
            {{ states('sensor.disk_monitor_critical_alerts') }} critical alert(s) detected.
            Check the Disk Monitor dashboard.
```

Notify when a RAID array starts resyncing:

```yaml
automation:
  - alias: "RAID md0 — resync started"
    trigger:
      - platform: state
        entity_id: sensor.raid_md0_state
        to: "resyncing"
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "ℹ️ RAID md0 resyncing"
          message: >
            md0 is resyncing.
            Progress: {{ states('sensor.raid_md0_resync_progress') }}%
```

Notify when a disk temperature gets dangerously high:

```yaml
automation:
  - alias: "Disk sda — high temperature"
    trigger:
      - platform: numeric_state
        entity_id: sensor.disk_sda_temperature
        above: 55
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "🌡️ Disk sda running hot"
          message: "sda temperature is {{ states('sensor.disk_sda_temperature') }}°C"
```

---

## Troubleshooting

### API server

**`command not found: smartctl`**

Install `smartmontools` — see [API server — requirements](#api-server--requirements).

**`command not found: mdadm`**

Install `mdadm`. If you have no RAID arrays, `/raids` returns an empty object `{}` — this is expected and not an error.

**`smartctl` returns no SMART data for a disk**

USB enclosures often block SMART passthrough. The disk appears in `/disks` with an `errors` entry explaining this. You can force a passthrough mode for some enclosures with `smartctl -d sat` but this is not configurable via the API.

**`sudo: smartctl: command not found` in logs**

The sudoers rule uses the full path. Find the correct path with `which smartctl` and update `/etc/sudoers.d/disk-monitor` to match.

**Port already in use**

Change `PORT` in `disk_monitor.conf` and restart the service.

**Swagger UI shows a lock icon but the Authorize dialog has no input**

This happens when `TOKEN` is empty — auth is disabled and no security scheme is registered. Set a token and restart.

---

### Home Assistant integration

**Integration does not appear in the catalogue after copying files**

Restart Home Assistant fully (not just a reload). HA only discovers new custom components on startup.

**"Cannot connect" error during setup**

Verify the API server is reachable from the HA host:

```bash
curl http://<disk-monitor-host>:8000/health
```

Check that `HOST = 0.0.0.0` is set in `disk_monitor.conf` so the server binds on all interfaces, not just localhost.

**"Invalid API key" error during setup**

Confirm the key in the HA form exactly matches `TOKEN` in `disk_monitor.conf`. The key is case-sensitive. If you recently changed the token, restart the API server — the config is cached at startup.

**Entities show as unavailable after setup**

HA marks entities unavailable when a coordinator update fails. Check **Settings → System → Logs** for errors from the `disk_monitor` integration. Common causes are a network interruption or the API server restarting.

**NVMe sensors do not appear for a spinning disk (or vice versa)**

This is correct behaviour. NVMe-specific sensors (wear, available spare, media errors) are only created when the API returns a non-null value for those fields on the first fetch. ATA SMART attributes (reallocated sectors, pending sectors, uncorrectable errors) are only created for ATA/SCSI disks.

**Entities are created but always show `unknown`**

The API is reachable but returning `null` for those fields. This usually means smartctl cannot read SMART data for that device (see USB enclosure note above). Check the `errors` field on the disk in `GET /disks/{name}`.

---

## License

MIT
