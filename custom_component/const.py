"""Constants for the Disk & RAID Monitor integration."""

DOMAIN = "disk_monitor"

# Config entry keys
CONF_HOST = "host"
CONF_PORT = "port"
CONF_API_KEY = "api_key"
CONF_USE_HTTPS = "use_https"
CONF_VERIFY_SSL = "verify_ssl"
CONF_SCAN_INTERVAL = "scan_interval"

# Defaults
DEFAULT_PORT = 8000
DEFAULT_USE_HTTPS = False
DEFAULT_VERIFY_SSL = True
DEFAULT_SCAN_INTERVAL = 60  # seconds

# Coordinator data keys
DATA_DISKS = "disks"
DATA_RAIDS = "raids"
DATA_ALERTS = "alerts"
DATA_COLLECTED_AT = "collected_at"
