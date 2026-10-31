"""Constants for the Aroma-Link Local integration."""

DOMAIN = "aroma_link_local"

# Options (OptionsFlow) with defaults.
CONF_QUERY_INTERVAL = "query_interval"      # seconds; 0 disables periodic state poll
CONF_UPLINK_WILDCARD = "uplink_wildcard"    # device->HA subscription
CONF_DOWNLINK_PATTERN = "downlink_pattern"  # HA->device, {id} = device topic id

DEFAULT_QUERY_INTERVAL = 0
DEFAULT_UPLINK_WILDCARD = "q/2/d/+/+"   # catches both /bus (control/state) and /sys (telemetry)
DEFAULT_DOWNLINK_PATTERN = "q/1/d/{id}/bus"

SIGNAL_NEW_DEVICE = f"{DOMAIN}_new_device"           # payload: AromaDevice
def signal_uplink(topic_id: str) -> str:              # per-device uplink events
    return f"{DOMAIN}_uplink_{topic_id}"
