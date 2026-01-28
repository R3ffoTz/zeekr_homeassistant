"""DataUpdateCoordinator for Zeekr EV API Integration."""

from __future__ import annotations

import asyncio
from datetime import timedelta, datetime
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import homeassistant.helpers.event as event


from .const import CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL, DOMAIN
from .journey_db import JourneyDatabase
from .request_stats import ZeekrRequestStats

if TYPE_CHECKING:
    # Import for type checking only
    try:
        from zeekr_ev_api.client import Vehicle, ZeekrClient
    except ImportError:
        from custom_components.zeekr_ev_api.client import Vehicle, ZeekrClient

_LOGGER = logging.getLogger(__name__)

# Nominatim API for reverse geocoding (free, 1 req/sec limit)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_USER_AGENT = "ZeekrHomeAssistant/1.0"


class ZeekrCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Zeekr data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: ZeekrClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        self.client = client
        self.entry = entry
        self.vehicles: list[Vehicle] = []
        # Shared settings for command durations
        self.seat_duration = 15
        self.ac_duration = 15
        self.steering_wheel_duration = 15
        self.request_stats = ZeekrRequestStats(hass)
        self.latest_poll_time: Optional[str] = None  # Track latest poll time

        # Initialize journey log database
        db_path = Path(hass.config.config_dir) / "zeekr_ev" / "journey_log.db"
        self.journey_db = JourneyDatabase(db_path)

        polling_interval = entry.data.get(CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=polling_interval),
        )

        # Schedule daily reset at midnight
        self._unsub_reset = None
        self._setup_daily_reset()

    def _setup_daily_reset(self):
        if self._unsub_reset:
            self._unsub_reset()
        self._unsub_reset = event.async_track_time_change(
            self.hass, self._handle_daily_reset, hour=0, minute=0, second=0
        )

    async def async_init_stats(self):
        """Initialize stats (load from storage)."""
        await self.request_stats.async_load()

    async def _handle_daily_reset(self, now):
        await self.request_stats.async_reset_today()

    def get_vehicle_by_vin(self, vin: str) -> Vehicle | None:
        """Get a vehicle by VIN."""
        for vehicle in self.vehicles:
            if vehicle.vin == vin:
                return vehicle
        return None

    async def _async_update_data(self) -> dict[str, dict]:
        """Fetch data from API endpoint."""
        try:
            # Refresh vehicle list if empty (first run)
            if not self.vehicles:
                await self.request_stats.async_inc_request()
                self.vehicles = await self.hass.async_add_executor_job(
                    self.client.get_vehicle_list
                )

            data = {}
            for vehicle in self.vehicles:
                try:
                    await self.request_stats.async_inc_request()
                    vehicle_data = await self.hass.async_add_executor_job(
                        vehicle.get_status
                    )
                except Exception as charge_err:
                    _LOGGER.error("Error fetching remote control status for %s: %s", vehicle.vin, charge_err)
                    # Skip this entire vehicle on error
                    continue

                # Fetch remote control status
                try:
                    await self.request_stats.async_inc_request()
                    vehicle_remote_state = await self.hass.async_add_executor_job(
                        vehicle.get_remote_control_state
                    )

                    if vehicle_remote_state:
                        vehicle_data.setdefault("additionalVehicleStatus", {})[
                            "remoteControlState"
                        ] = vehicle_remote_state
                except Exception as charge_err:
                    _LOGGER.debug("Error fetching remote control status for %s: %s", vehicle.vin, charge_err)

                # Fetch charging status
                try:
                    await self.request_stats.async_inc_request()
                    charging_status = await self.hass.async_add_executor_job(
                        vehicle.get_charging_status
                    )
                    if charging_status:
                        vehicle_data.setdefault("chargingStatus", {}).update(charging_status)
                except Exception as charge_err:
                    _LOGGER.debug("Error fetching charging status for %s: %s", vehicle.vin, charge_err)

                # Fetch charging limit
                try:
                    await self.request_stats.async_inc_request()
                    charging_limit = await self.hass.async_add_executor_job(
                        vehicle.get_charging_limit
                    )
                    if charging_limit:
                        vehicle_data["chargingLimit"] = charging_limit
                except Exception as limit_err:
                    _LOGGER.debug("Error fetching charging limit for %s: %s", vehicle.vin, limit_err)

                # Fetch charge plan
                try:
                    if hasattr(vehicle, "get_charge_plan"):
                        await self.request_stats.async_inc_request()
                        charge_plan = await self.hass.async_add_executor_job(
                            vehicle.get_charge_plan
                        )
                        if charge_plan:
                            vehicle_data["chargePlan"] = charge_plan
                except Exception as plan_err:
                    _LOGGER.debug("Error fetching charge plan for %s: %s", vehicle.vin, plan_err)

                # Fetch travel plan
                try:
                    if hasattr(vehicle, "get_travel_plan"):
                        await self.request_stats.async_inc_request()
                        travel_plan = await self.hass.async_add_executor_job(
                            vehicle.get_travel_plan
                        )
                        if travel_plan:
                            vehicle_data["travelPlan"] = travel_plan
                except Exception as travel_err:
                    _LOGGER.debug("Error fetching travel plan for %s: %s", vehicle.vin, travel_err)

                # Fetch journey log
                try:
                    if hasattr(vehicle, "get_journey_log"):
                        await self.request_stats.async_inc_request()
                        journey_log = await self.hass.async_add_executor_job(
                            lambda v=vehicle: v.get_journey_log(page_size=50)
                        )
                        if journey_log:
                            vehicle_data["journeyLog"] = journey_log
                            # Save new trips to database
                            await self._save_new_trips(vehicle.vin, journey_log)
                except Exception as journey_err:
                    _LOGGER.debug("Error fetching journey log for %s: %s", vehicle.vin, journey_err)

                data[vehicle.vin] = vehicle_data

            # Update latest poll time on every automatic poll
            self.latest_poll_time = datetime.now().isoformat()

        except Exception as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        else:
            return data

    async def _save_new_trips(self, vin: str, journey_log: dict[str, Any]) -> None:
        """Save new trips from journey log to database."""
        trips_raw = journey_log.get("data", [])
        if not trips_raw:
            return

        def save_trips():
            added = 0
            new_trip_ids = []
            for trip in trips_raw:
                trip_data = self._convert_trip_to_db_format(vin, trip)
                if self.journey_db.add_trip(trip_data):
                    added += 1
                    new_trip_ids.append(trip_data.get("trip_id"))
            return added, new_trip_ids

        try:
            added, new_trip_ids = await self.hass.async_add_executor_job(save_trips)
            if added > 0:
                _LOGGER.info("Saved %d new trip(s) to journey database for %s", added, vin[-4:])
                # Schedule geocoding for new trips in background
                self.hass.async_create_task(
                    self._geocode_trips_without_addresses(vin)
                )
        except Exception as err:
            _LOGGER.error("Error saving trips to database: %s", err)

    def _convert_trip_to_db_format(self, vin: str, trip: dict[str, Any]) -> dict[str, Any]:
        """Convert API trip data to database format."""
        track_points = trip.get("trackPoints", [])
        start_point = track_points[0] if track_points else {}
        end_point = track_points[-1] if track_points else {}

        # Calculate duration in minutes
        start_ts = trip.get("startTime", 0)
        end_ts = trip.get("endTime", 0)
        duration_min = round((end_ts - start_ts) / 60000) if end_ts and start_ts else None

        return {
            "vin": vin,
            "trip_id": trip.get("tripId"),
            "report_time": trip.get("reportTime"),
            "start_time": start_ts,
            "end_time": end_ts,
            "duration_min": duration_min,
            "distance_km": trip.get("traveledDistance"),
            "avg_speed_kmh": trip.get("avgSpeed"),
            "consumption_kwh": trip.get("electricConsumption"),
            "regeneration_wh": trip.get("electricRegeneration"),
            "start_lat": start_point.get("latitude"),
            "start_lon": start_point.get("longitude"),
            "end_lat": end_point.get("latitude"),
            "end_lon": end_point.get("longitude"),
            "start_odometer": trip.get("startOdometer"),
            "end_odometer": trip.get("endOdometer"),
        }

    async def _reverse_geocode(self, lat: float, lon: float) -> str | None:
        """Reverse geocode coordinates to address using Nominatim.
        
        Args:
            lat: Latitude
            lon: Longitude
            
        Returns:
            Human-readable address or None if failed.
        """
        if not lat or not lon:
            return None

        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    "format": "json",
                    "lat": lat,
                    "lon": lon,
                    "zoom": 18,
                    "addressdetails": 1,
                }
                headers = {"User-Agent": NOMINATIM_USER_AGENT}
                
                async with session.get(
                    NOMINATIM_URL, params=params, headers=headers, timeout=10
                ) as response:
                    if response.status != 200:
                        _LOGGER.debug("Geocoding failed with status %s", response.status)
                        return None

                    data = await response.json()
                    
                    if not data or "address" not in data:
                        return data.get("display_name")

                    addr = data["address"]
                    parts = []

                    # Street address
                    if addr.get("road"):
                        if addr.get("house_number"):
                            parts.append(f"{addr['road']} {addr['house_number']}")
                        else:
                            parts.append(addr["road"])

                    # City/town
                    city = (
                        addr.get("city")
                        or addr.get("town")
                        or addr.get("village")
                        or addr.get("municipality")
                    )
                    if city:
                        parts.append(city)

                    return ", ".join(parts) if parts else data.get("display_name")

        except asyncio.TimeoutError:
            _LOGGER.debug("Geocoding timeout for %s, %s", lat, lon)
            return None
        except Exception as err:
            _LOGGER.debug("Geocoding error: %s", err)
            return None

    async def _geocode_trips_without_addresses(self, vin: str | None = None) -> None:
        """Geocode trips that don't have addresses yet.
        
        Args:
            vin: Optional VIN to filter trips. If None, geocode all.
        """
        try:
            # Get trips without addresses
            trips = await self.hass.async_add_executor_job(
                self.journey_db.get_trips_without_addresses, vin, 10
            )

            if not trips:
                return

            _LOGGER.debug("Geocoding addresses for %d trip(s)", len(trips))
            geocoded = 0

            for trip in trips:
                trip_id = trip.get("id")
                start_address = None
                end_address = None

                # Geocode start address if missing
                if not trip.get("start_address") and trip.get("start_lat") and trip.get("start_lon"):
                    start_address = await self._reverse_geocode(
                        trip["start_lat"], trip["start_lon"]
                    )
                    # Respect rate limit (1 req/sec)
                    await asyncio.sleep(1.1)

                # Geocode end address if missing
                if not trip.get("end_address") and trip.get("end_lat") and trip.get("end_lon"):
                    end_address = await self._reverse_geocode(
                        trip["end_lat"], trip["end_lon"]
                    )
                    # Respect rate limit
                    await asyncio.sleep(1.1)

                # Update database if we got any addresses
                if start_address or end_address:
                    await self.hass.async_add_executor_job(
                        self.journey_db.update_trip,
                        trip_id,
                        None,  # purpose
                        None,  # category
                        None,  # notes
                        start_address,
                        end_address,
                    )
                    geocoded += 1
                    _LOGGER.debug(
                        "Geocoded trip %s: start='%s', end='%s'",
                        trip_id, start_address, end_address
                    )

            if geocoded > 0:
                _LOGGER.info("Geocoded addresses for %d trip(s)", geocoded)

        except Exception as err:
            _LOGGER.error("Error geocoding trips: %s", err)

    async def async_inc_invoke(self):
        await self.request_stats.async_inc_invoke()

    async def async_shutdown(self) -> None:
        """Shutdown coordinator and close database."""
        if self._unsub_reset:
            self._unsub_reset()
        await self.request_stats.async_shutdown()
        await self.hass.async_add_executor_job(self.journey_db.close)
