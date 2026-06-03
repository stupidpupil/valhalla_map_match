"""Constants for the Valhalla Map Match integration."""

DOMAIN = "valhalla_map_match"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_SSL = "ssl"

DEFAULT_PORT = 8002
DEFAULT_COSTING = "auto"
DEFAULT_TIME_WINDOW = 10  # minutes
DEFAULT_MAX_POINTS = 6
DEFAULT_ATTRIBUTES = ["edge.names", "edge.end_heading"]

SERVICE_MAP_MATCH = "map_match"

COSTING_MODELS = [
    "auto",
    "pedestrian",
    "bicycle",
    "bus",
    "truck",
    "motor_scooter",
    "motorcycle",
]

XCLIENTID = "https://github.com/stupidpupil/ha-valhalla-map-match"