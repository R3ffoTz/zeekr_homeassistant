"""Custom integration to integrate Zeekr EV API Integration with Home Assistant.

For more details about this integration, please refer to
https://github.com/Fryyyyy/zeekr_homeassistant
"""

import logging
import importlib

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.typing import ConfigType
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_HMAC_ACCESS_KEY,
    CONF_HMAC_SECRET_KEY,
    CONF_PASSWORD,
    CONF_PASSWORD_PUBLIC_KEY,
    CONF_PROD_SECRET,
    CONF_USERNAME,
    CONF_VIN_IV,
    CONF_VIN_KEY,
    CONF_COUNTRY_CODE,
    CONF_USE_LOCAL_API,
    DOMAIN,
    PLATFORMS,
    STARTUP_MESSAGE,
)
from .coordinator import ZeekrCoordinator
from .request_stats import ZeekrRequestStats

_LOGGER: logging.Logger = logging.getLogger(__package__)

# Service constants
SERVICE_GET_TRIP_TRACKPOINTS = "get_trip_trackpoints"
SERVICE_GET_STORED_TRIPS = "get_stored_trips"
SERVICE_UPDATE_TRIP = "update_trip"
SERVICE_GET_TRIP_STATISTICS = "get_trip_statistics"

ATTR_VIN = "vin"
ATTR_TRIP_ID = "trip_id"
ATTR_TRIP_REPORT_TIME = "trip_report_time"
ATTR_LIMIT = "limit"
ATTR_OFFSET = "offset"
ATTR_CATEGORY = "category"
ATTR_START_DATE = "start_date"
ATTR_END_DATE = "end_date"
ATTR_DB_ID = "db_id"
ATTR_PURPOSE = "purpose"
ATTR_NOTES = "notes"
ATTR_START_ADDRESS = "start_address"
ATTR_END_ADDRESS = "end_address"

# Service schemas
SERVICE_GET_TRIP_TRACKPOINTS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_VIN): cv.string,
        vol.Required(ATTR_TRIP_ID): cv.positive_int,
        vol.Required(ATTR_TRIP_REPORT_TIME): cv.positive_int,
    }
)

SERVICE_GET_STORED_TRIPS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_VIN): cv.string,
        vol.Optional(ATTR_CATEGORY): cv.string,
        vol.Optional(ATTR_START_DATE): cv.string,
        vol.Optional(ATTR_END_DATE): cv.string,
        vol.Optional(ATTR_LIMIT, default=100): cv.positive_int,
        vol.Optional(ATTR_OFFSET, default=0): vol.Coerce(int),
    }
)

SERVICE_UPDATE_TRIP_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DB_ID): cv.positive_int,
        vol.Optional(ATTR_PURPOSE): cv.string,
        vol.Optional(ATTR_CATEGORY): cv.string,
        vol.Optional(ATTR_NOTES): cv.string,
        vol.Optional(ATTR_START_ADDRESS): cv.string,
        vol.Optional(ATTR_END_ADDRESS): cv.string,
    }
)

SERVICE_GET_TRIP_STATISTICS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_VIN): cv.string,
        vol.Optional(ATTR_START_DATE): cv.string,
        vol.Optional(ATTR_END_DATE): cv.string,
    }
)


def get_zeekr_client_class(use_local: bool = False):
    """Dynamically import ZeekrClient from local or installed package."""
    if use_local:
        try:
            # Try to import from local custom_components folder
            module = importlib.import_module("custom_components.zeekr_ev_api.client")
            _LOGGER.debug("Using local zeekr_ev_api from custom_components")
            return module.ZeekrClient
        except ImportError as ex:
            raise ImportError(
                "Local zeekr_ev_api not found in custom_components. "
                "Please install it or disable 'Use local API' option."
            ) from ex

    # Try to import from installed package (pip)
    try:
        module = importlib.import_module("zeekr_ev_api.client")
        _LOGGER.debug("Using installed zeekr_ev_api package")
        return module.ZeekrClient
    except ImportError as ex:
        raise ImportError(
            "zeekr_ev_api package not installed. "
            "Please install it via pip or enable 'Use local API' option."
        ) from ex


async def async_setup(hass: HomeAssistant, config: ConfigType):
    """Set up this integration using YAML is not supported."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up this integration using UI."""
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
        _LOGGER.info(STARTUP_MESSAGE)

    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)
    country_code = entry.data.get(CONF_COUNTRY_CODE, "")
    hmac_access_key = entry.data.get(CONF_HMAC_ACCESS_KEY, "")
    hmac_secret_key = entry.data.get(CONF_HMAC_SECRET_KEY, "")
    password_public_key = entry.data.get(CONF_PASSWORD_PUBLIC_KEY, "")
    prod_secret = entry.data.get(CONF_PROD_SECRET, "")
    vin_key = entry.data.get(CONF_VIN_KEY, "")
    vin_iv = entry.data.get(CONF_VIN_IV, "'")

    if not username or not password:
        _LOGGER.warning("No username or password")
        return False

    use_local_api = entry.data.get(CONF_USE_LOCAL_API, False)

    # Run import in executor to avoid blocking the event loop
    try:
        ZeekrClient = await hass.async_add_executor_job(
            get_zeekr_client_class, use_local_api
        )
    except ImportError as ex:
        _LOGGER.error("Failed to import zeekr_ev_api: %s", ex)
        raise ConfigEntryNotReady from ex

    # Try to reuse client from config flow to avoid duplicate login
    client = hass.data.get(DOMAIN, {}).pop("_temp_client", None)

    if client is None or not client.logged_in:
        client = ZeekrClient(
            username=username,
            password=password,
            country_code=country_code,
            hmac_access_key=hmac_access_key,
            hmac_secret_key=hmac_secret_key,
            password_public_key=password_public_key,
            prod_secret=prod_secret,
            vin_key=vin_key,
            vin_iv=vin_iv,
            logger=_LOGGER,
        )
        try:
            # Count the login request
            stats = ZeekrRequestStats(hass)
            await stats.async_load()
            await stats.async_inc_request()
            await hass.async_add_executor_job(client.login)
        except Exception as ex:
            _LOGGER.error("Could not log in to Zeekr API: %s", ex)
            raise ConfigEntryNotReady from ex

    coordinator = ZeekrCoordinator(hass, client=client, entry=entry)
    await coordinator.async_init_stats()
    await coordinator.async_config_entry_first_refresh()

    if coordinator.vehicles:
        _LOGGER.info(
            "Found %d vehicle(s): %s",
            len(coordinator.vehicles),
            ", ".join(v.vin for v in coordinator.vehicles),
        )
    else:
        _LOGGER.warning("No vehicles found in account")

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services (only once)
    await async_setup_services(hass)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for the Zeekr integration."""

    async def async_get_trip_trackpoints(call: ServiceCall) -> ServiceResponse:
        """Handle the get_trip_trackpoints service call."""
        vin = call.data[ATTR_VIN]
        trip_id = call.data[ATTR_TRIP_ID]
        trip_report_time = call.data[ATTR_TRIP_REPORT_TIME]

        # Find the coordinator for this VIN
        coordinator = None
        for entry_id, coord in hass.data[DOMAIN].items():
            if entry_id.startswith("_"):
                continue
            if isinstance(coord, ZeekrCoordinator):
                for vehicle in coord.vehicles:
                    if vehicle.vin == vin:
                        coordinator = coord
                        break
            if coordinator:
                break

        if not coordinator:
            raise HomeAssistantError(f"Vehicle with VIN {vin} not found")

        vehicle = coordinator.get_vehicle_by_vin(vin)
        if not vehicle:
            raise HomeAssistantError(f"Vehicle with VIN {vin} not found")

        try:
            # Increment API request counter
            await coordinator.request_stats.async_inc_request()

            # Fetch trackpoints
            trackpoints = await hass.async_add_executor_job(
                vehicle.get_trip_trackpoints, trip_report_time, trip_id
            )

            _LOGGER.debug(
                "Fetched %d trackpoints for trip %d", len(trackpoints), trip_id
            )

            return {
                "vin": vin,
                "trip_id": trip_id,
                "trip_report_time": trip_report_time,
                "trackpoints": trackpoints,
                "count": len(trackpoints),
            }

        except Exception as ex:
            _LOGGER.error("Failed to fetch trip trackpoints: %s", ex)
            raise HomeAssistantError(f"Failed to fetch trackpoints: {ex}") from ex

    async def async_get_stored_trips(call: ServiceCall) -> ServiceResponse:
        """Handle the get_stored_trips service call."""
        from datetime import datetime

        vin = call.data.get(ATTR_VIN)
        category = call.data.get(ATTR_CATEGORY)
        start_date_str = call.data.get(ATTR_START_DATE)
        end_date_str = call.data.get(ATTR_END_DATE)
        limit = call.data.get(ATTR_LIMIT, 100)
        offset = call.data.get(ATTR_OFFSET, 0)

        # Parse dates if provided
        start_date = None
        end_date = None
        if start_date_str:
            try:
                start_date = datetime.fromisoformat(start_date_str)
            except ValueError:
                raise HomeAssistantError(f"Invalid start_date format: {start_date_str}")
        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str)
            except ValueError:
                raise HomeAssistantError(f"Invalid end_date format: {end_date_str}")

        # Find any coordinator to access the database
        coordinator = None
        for entry_id, coord in hass.data[DOMAIN].items():
            if entry_id.startswith("_"):
                continue
            if isinstance(coord, ZeekrCoordinator):
                coordinator = coord
                break

        if not coordinator:
            raise HomeAssistantError("No Zeekr integration found")

        try:
            trips = await hass.async_add_executor_job(
                coordinator.journey_db.get_trips,
                vin,
                category,
                start_date,
                end_date,
                limit,
                offset,
            )

            return {
                "trips": trips,
                "count": len(trips),
                "limit": limit,
                "offset": offset,
            }

        except Exception as ex:
            _LOGGER.error("Failed to get stored trips: %s", ex)
            raise HomeAssistantError(f"Failed to get stored trips: {ex}") from ex

    async def async_update_trip(call: ServiceCall) -> ServiceResponse:
        """Handle the update_trip service call."""
        db_id = call.data[ATTR_DB_ID]
        purpose = call.data.get(ATTR_PURPOSE)
        category = call.data.get(ATTR_CATEGORY)
        notes = call.data.get(ATTR_NOTES)
        start_address = call.data.get(ATTR_START_ADDRESS)
        end_address = call.data.get(ATTR_END_ADDRESS)

        # Find any coordinator to access the database
        coordinator = None
        for entry_id, coord in hass.data[DOMAIN].items():
            if entry_id.startswith("_"):
                continue
            if isinstance(coord, ZeekrCoordinator):
                coordinator = coord
                break

        if not coordinator:
            raise HomeAssistantError("No Zeekr integration found")

        try:
            success = await hass.async_add_executor_job(
                coordinator.journey_db.update_trip,
                db_id,
                purpose,
                category,
                notes,
                start_address,
                end_address,
            )

            return {
                "success": success,
                "db_id": db_id,
            }

        except Exception as ex:
            _LOGGER.error("Failed to update trip: %s", ex)
            raise HomeAssistantError(f"Failed to update trip: {ex}") from ex

    async def async_get_trip_statistics(call: ServiceCall) -> ServiceResponse:
        """Handle the get_trip_statistics service call."""
        from datetime import datetime

        vin = call.data.get(ATTR_VIN)
        start_date_str = call.data.get(ATTR_START_DATE)
        end_date_str = call.data.get(ATTR_END_DATE)

        # Parse dates if provided
        start_date = None
        end_date = None
        if start_date_str:
            try:
                start_date = datetime.fromisoformat(start_date_str)
            except ValueError:
                raise HomeAssistantError(f"Invalid start_date format: {start_date_str}")
        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str)
            except ValueError:
                raise HomeAssistantError(f"Invalid end_date format: {end_date_str}")

        # Find any coordinator to access the database
        coordinator = None
        for entry_id, coord in hass.data[DOMAIN].items():
            if entry_id.startswith("_"):
                continue
            if isinstance(coord, ZeekrCoordinator):
                coordinator = coord
                break

        if not coordinator:
            raise HomeAssistantError("No Zeekr integration found")

        try:
            stats = await hass.async_add_executor_job(
                coordinator.journey_db.get_statistics,
                vin,
                start_date,
                end_date,
            )

            # Also get category breakdown
            uncategorized = await hass.async_add_executor_job(
                coordinator.journey_db.get_uncategorized_count,
                vin,
            )

            stats["uncategorized_trips"] = uncategorized

            return stats

        except Exception as ex:
            _LOGGER.error("Failed to get trip statistics: %s", ex)
            raise HomeAssistantError(f"Failed to get statistics: {ex}") from ex

    # Only register if not already registered
    if not hass.services.has_service(DOMAIN, SERVICE_GET_TRIP_TRACKPOINTS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_TRIP_TRACKPOINTS,
            async_get_trip_trackpoints,
            schema=SERVICE_GET_TRIP_TRACKPOINTS_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
        _LOGGER.debug("Registered service: %s.%s", DOMAIN, SERVICE_GET_TRIP_TRACKPOINTS)

    if not hass.services.has_service(DOMAIN, SERVICE_GET_STORED_TRIPS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_STORED_TRIPS,
            async_get_stored_trips,
            schema=SERVICE_GET_STORED_TRIPS_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
        _LOGGER.debug("Registered service: %s.%s", DOMAIN, SERVICE_GET_STORED_TRIPS)

    if not hass.services.has_service(DOMAIN, SERVICE_UPDATE_TRIP):
        hass.services.async_register(
            DOMAIN,
            SERVICE_UPDATE_TRIP,
            async_update_trip,
            schema=SERVICE_UPDATE_TRIP_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
        _LOGGER.debug("Registered service: %s.%s", DOMAIN, SERVICE_UPDATE_TRIP)

    if not hass.services.has_service(DOMAIN, SERVICE_GET_TRIP_STATISTICS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_TRIP_STATISTICS,
            async_get_trip_statistics,
            schema=SERVICE_GET_TRIP_STATISTICS_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
        _LOGGER.debug("Registered service: %s.%s", DOMAIN, SERVICE_GET_TRIP_STATISTICS)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    coordinator = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator:
        await coordinator.async_shutdown()

    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    # Unregister services if no more entries
    remaining_entries = [
        k for k in hass.data.get(DOMAIN, {}).keys() if not k.startswith("_")
    ]
    if not remaining_entries:
        for service_name in [
            SERVICE_GET_TRIP_TRACKPOINTS,
            SERVICE_GET_STORED_TRIPS,
            SERVICE_UPDATE_TRIP,
            SERVICE_GET_TRIP_STATISTICS,
        ]:
            if hass.services.has_service(DOMAIN, service_name):
                hass.services.async_remove(DOMAIN, service_name)
                _LOGGER.debug("Unregistered service: %s.%s", DOMAIN, service_name)

    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
