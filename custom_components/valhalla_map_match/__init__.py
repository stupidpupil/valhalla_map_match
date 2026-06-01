"""The Valhalla Map Match integration."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from functools import partial
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
from homeassistant.util import dt as dt_util

# ServiceValidationError was introduced in HA 2024.1; fall back to the base
# HomeAssistantError for anyone on an older release.
try:
    from homeassistant.exceptions import ServiceValidationError
except ImportError:
    from homeassistant.exceptions import HomeAssistantError as ServiceValidationError  # type: ignore[assignment]

from .const import (
    COSTING_MODELS,
    CONF_HOST,
    CONF_PORT,
    CONF_SSL,
    DEFAULT_COSTING,
    DEFAULT_ATTRIBUTES,
    DEFAULT_MAX_POINTS,
    DEFAULT_TIME_WINDOW,
    DOMAIN,
    SERVICE_MAP_MATCH,
    XCLIENTID
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Service schema
# ---------------------------------------------------------------------------

def _parse_timestamp(value: Any) -> datetime:
    """Accept a datetime object or an ISO-8601 string."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        parsed = dt_util.parse_datetime(value)
        if parsed is not None:
            return parsed
    raise vol.Invalid(f"Invalid datetime value: {value!r}")


SERVICE_MAP_MATCH_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Optional("timestamp"): _parse_timestamp,
        vol.Optional("time_window", default=DEFAULT_TIME_WINDOW): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=60*24*7)
        ),
        vol.Optional("max_points", default=DEFAULT_MAX_POINTS): vol.All(
            vol.Coerce(int), vol.Range(min=2, max=128)
        ),
        vol.Optional("costing", default=DEFAULT_COSTING): vol.In(COSTING_MODELS),
        vol.Optional("attributes", default=DEFAULT_ATTRIBUTES): vol.All(
            cv.ensure_list, [cv.string]
        ),
    }
)


# ---------------------------------------------------------------------------
# Integration setup / teardown
# ---------------------------------------------------------------------------

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Valhalla Map Match from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = dict(entry.data)

    protocol = "https" if entry.data[CONF_SSL] else "http"
    base_url = f"{protocol}://{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}"

    # ------------------------------------------------------------------
    # Service handler
    # ------------------------------------------------------------------

    async def handle_map_match(call: ServiceCall) -> ServiceResponse:
        """Handle valhalla_map_match.map_match service calls."""
        entity_id: str = call.data["entity_id"]
        time_window: int = call.data["time_window"]
        max_points: int = call.data["max_points"]
        costing: str = call.data["costing"]
        attributes: list[str] = call.data["attributes"]

        # ---- 1. Resolve the reference timestamp -------------------------

        timestamp: datetime | None = call.data.get("timestamp")
        if timestamp is None:
            state = hass.states.get(entity_id)
            if state is None:
                raise ServiceValidationError(
                    f"Device tracker entity '{entity_id}' was not found."
                )
            timestamp = state.last_updated

        # Ensure the timestamp is timezone-aware (UTC) before arithmetic.
        if timestamp.tzinfo is None:
            timestamp = dt_util.as_utc(timestamp)

        start_time = timestamp - timedelta(minutes=time_window)

        _LOGGER.debug(
            "map_match: fetching history for %s from %s to %s",
            entity_id,
            start_time.isoformat(),
            timestamp.isoformat(),
        )

        # ---- 2. Pull location history from the recorder -----------------

        # significant_changes_only=False ensures we capture every GPS update,
        # not just transitions between named states (e.g. home → not_home).
        # include_start_time_state=False means we only get states that were
        # actually recorded within the window, avoiding a synthetic "carry-
        # forward" entry from before start_time.
        recorder_instance = get_instance(hass)
        history: dict[str, list] = await recorder_instance.async_add_executor_job(
            partial(
                get_significant_states,
                hass,
                start_time,
                timestamp,
                [entity_id],
                significant_changes_only=False,
                include_start_time_state=False,
                minimal_response=False,
            )
        )

        states = history.get(entity_id, [])
        _LOGGER.debug(
            "map_match: %d history state(s) for %s", len(states), entity_id
        )

        # ---- 3. Filter to states that carry GPS co-ordinates ------------

        located = [
            s
            for s in states
            if s.attributes.get("latitude") is not None
            and s.attributes.get("longitude") is not None
        ]

        if len(located) < 2:
            raise ServiceValidationError(
                f"Not enough GPS data for '{entity_id}': found {len(located)} "
                f"located state(s) in the last {time_window} minute(s) "
                f"(need at least 2 for map matching)."
            )

        # ---- 4. Select the most recent max_points entries ---------------

        selected = located[-max_points:]

        shape = [
            {
                "lat": s.attributes["latitude"], 
                "lon": s.attributes["longitude"],
                "time": dt_util.as_utc(s.last_updated).timestamp() - start_time.timestamp()
            }
            for s in selected
        ]

        _LOGGER.debug(
            "map_match: sending %d shape point(s) to Valhalla (%s costing)",
            len(shape),
            costing,
        )

        # ---- 5. Call Valhalla trace_attributes --------------------------

        payload = {
            "shape": shape,
            "costing": costing,
            "shape_match": "map_snap",
            "use_timestamps": True,
            "filters": {
                "attributes": attributes,
                "action": "include",
            },
        }

        session = async_get_clientsession(hass)
        try:
            async with session.post(
                f"{base_url}/trace_attributes",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
                headers={'X-Client-ID': XCLIENTID}
            ) as resp:
                # Parse the body regardless of status so we can surface
                # Valhalla's own error message when things go wrong.
                response_data: dict = await resp.json(content_type=None)

                if resp.status != 200:
                    valhalla_error = response_data.get("error", resp.reason)
                    raise ServiceValidationError(
                        f"Valhalla returned HTTP {resp.status}: {valhalla_error}"
                    )

        except aiohttp.ClientError as err:
            raise ServiceValidationError(
                f"Could not reach Valhalla at {base_url}: {err}"
            ) from err

        # ---- 6. Return structured response data -------------------------

        return {
            "edges": response_data.get("edges", []),
            "matched_points": response_data.get("matched_points", []),
            "shape": response_data.get("shape", ""),
            "entity_id": entity_id,
            "costing": costing,
            "shape_point_count": len(shape),
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_MAP_MATCH,
        handle_map_match,
        schema=SERVICE_MAP_MATCH_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    _LOGGER.info("Valhalla Map Match ready (base URL: %s)", base_url)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.services.async_remove(DOMAIN, SERVICE_MAP_MATCH)
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return True
