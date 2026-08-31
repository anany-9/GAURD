import os
import json

# Data Storage Paths
DATA_DIR = "data"
CSV_WORKERS = os.path.join(DATA_DIR, "workers_registry.csv")
CSV_NODES = os.path.join(DATA_DIR, "scanner_nodes.csv")
CSV_WEARABLES = os.path.join(DATA_DIR, "wearable_devices.csv")
CSV_HISTORY = os.path.join(DATA_DIR, "location_history.csv")
CONFIG_FILE = os.path.join(DATA_DIR, "system_config.json")
AUDIO_TEMP_FILE = "temp_broadcast.mp3"

# Default System Config
DEFAULT_CONFIG = {
    "rtls_major_filter": "1217",
    "rtls_minor_filter": "23",
    "poll_interval_rtls": 0.5,
    "poll_interval_wearables": 2.0,
    "detection_timeout": 120,
    "api_timeout": 4.0
}

def load_config():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except:
            return DEFAULT_CONFIG
    return DEFAULT_CONFIG

def save_config(config_data):
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config_data, f, indent=2)

SYSTEM_CONFIG = load_config()