"""SQLite database for persistent journey log storage."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Database schema version - increment when schema changes
DB_VERSION = 1


class JourneyDatabase:
    """SQLite database for storing journey log entries."""

    def __init__(self, db_path: str | Path) -> None:
        """Initialize the database.

        Args:
            db_path: Path to the SQLite database file.
        """
        self.db_path = Path(db_path)
        self._connection: sqlite3.Connection | None = None

    def _get_connection(self) -> sqlite3.Connection:
        """Get or create database connection."""
        if self._connection is None:
            # Ensure parent directory exists
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

            self._connection = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._init_schema()

        return self._connection

    def _init_schema(self) -> None:
        """Initialize database schema."""
        conn = self._connection
        if conn is None:
            return

        cursor = conn.cursor()

        # Check if we need to create/migrate schema
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        if cursor.fetchone() is None:
            # First time setup
            self._create_tables(cursor)
            cursor.execute(
                "CREATE TABLE schema_version (version INTEGER PRIMARY KEY)"
            )
            cursor.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (DB_VERSION,)
            )
            conn.commit()
            _LOGGER.info("Created journey log database at %s", self.db_path)
        else:
            # Check version for future migrations
            cursor.execute("SELECT version FROM schema_version")
            row = cursor.fetchone()
            current_version = row[0] if row else 0
            if current_version < DB_VERSION:
                self._migrate_schema(cursor, current_version)
                cursor.execute(
                    "UPDATE schema_version SET version = ?", (DB_VERSION,)
                )
                conn.commit()

    def _create_tables(self, cursor: sqlite3.Cursor) -> None:
        """Create database tables."""
        # Main trips table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vin TEXT NOT NULL,
                trip_id INTEGER NOT NULL,
                report_time INTEGER,
                start_time INTEGER,
                end_time INTEGER,
                duration_min INTEGER,
                distance_km REAL,
                avg_speed_kmh REAL,
                consumption_kwh REAL,
                regeneration_wh REAL,
                start_lat REAL,
                start_lon REAL,
                end_lat REAL,
                end_lon REAL,
                start_odometer REAL,
                end_odometer REAL,
                
                -- User-editable fields for journey log purposes
                purpose TEXT DEFAULT NULL,
                category TEXT DEFAULT 'uncategorized',
                notes TEXT DEFAULT NULL,
                start_address TEXT DEFAULT NULL,
                end_address TEXT DEFAULT NULL,
                
                -- Metadata
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                
                -- Unique constraint to prevent duplicates
                UNIQUE(vin, trip_id, report_time)
            )
        """)

        # Index for common queries
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_trips_vin ON trips(vin)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_trips_start_time ON trips(start_time)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_trips_category ON trips(category)"
        )

        _LOGGER.debug("Created trips table")

    def _migrate_schema(
        self, cursor: sqlite3.Cursor, from_version: int
    ) -> None:
        """Migrate schema from older version."""
        # Future migrations go here
        # if from_version < 2:
        #     cursor.execute("ALTER TABLE trips ADD COLUMN new_field TEXT")
        _LOGGER.info(
            "Migrated journey database from version %d to %d",
            from_version,
            DB_VERSION,
        )

    def trip_exists(self, vin: str, trip_id: int, report_time: int | None) -> bool:
        """Check if a trip already exists in the database.

        Args:
            vin: Vehicle identification number.
            trip_id: Trip ID from the API.
            report_time: Report timestamp from the API.

        Returns:
            True if trip exists, False otherwise.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        if report_time:
            cursor.execute(
                "SELECT 1 FROM trips WHERE vin = ? AND trip_id = ? AND report_time = ?",
                (vin, trip_id, report_time),
            )
        else:
            cursor.execute(
                "SELECT 1 FROM trips WHERE vin = ? AND trip_id = ?",
                (vin, trip_id),
            )

        return cursor.fetchone() is not None

    def add_trip(self, trip_data: dict[str, Any]) -> int | None:
        """Add a new trip to the database.

        Args:
            trip_data: Dictionary with trip data.

        Returns:
            The row ID of the inserted trip, or None if it already exists.
        """
        vin = trip_data.get("vin")
        trip_id = trip_data.get("trip_id")
        report_time = trip_data.get("report_time")

        if not vin or trip_id is None:
            _LOGGER.warning("Cannot add trip: missing vin or trip_id")
            return None

        if self.trip_exists(vin, trip_id, report_time):
            _LOGGER.debug(
                "Trip %s/%s already exists, skipping", vin[-4:], trip_id
            )
            return None

        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                """
                INSERT INTO trips (
                    vin, trip_id, report_time, start_time, end_time,
                    duration_min, distance_km, avg_speed_kmh,
                    consumption_kwh, regeneration_wh,
                    start_lat, start_lon, end_lat, end_lon,
                    start_odometer, end_odometer
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    vin,
                    trip_id,
                    report_time,
                    trip_data.get("start_time"),
                    trip_data.get("end_time"),
                    trip_data.get("duration_min"),
                    trip_data.get("distance_km"),
                    trip_data.get("avg_speed_kmh"),
                    trip_data.get("consumption_kwh"),
                    trip_data.get("regeneration_wh"),
                    trip_data.get("start_lat"),
                    trip_data.get("start_lon"),
                    trip_data.get("end_lat"),
                    trip_data.get("end_lon"),
                    trip_data.get("start_odometer"),
                    trip_data.get("end_odometer"),
                ),
            )
            conn.commit()
            row_id = cursor.lastrowid
            _LOGGER.info(
                "Added new trip to database: VIN=%s, trip_id=%s, distance=%.1f km",
                vin[-4:] if vin else "?",
                trip_id,
                trip_data.get("distance_km") or 0,
            )
            return row_id

        except sqlite3.IntegrityError:
            # Race condition - trip was added between check and insert
            _LOGGER.debug("Trip already exists (race condition)")
            return None

    def add_trips_batch(self, trips: list[dict[str, Any]]) -> int:
        """Add multiple trips, skipping duplicates.

        Args:
            trips: List of trip data dictionaries.

        Returns:
            Number of trips actually added.
        """
        added = 0
        for trip in trips:
            if self.add_trip(trip):
                added += 1
        return added

    def get_trips(
        self,
        vin: str | None = None,
        category: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query trips from the database.

        Args:
            vin: Filter by vehicle (optional).
            category: Filter by category (optional).
            start_date: Filter trips starting after this date (optional).
            end_date: Filter trips starting before this date (optional).
            limit: Maximum number of trips to return.
            offset: Number of trips to skip.

        Returns:
            List of trip dictionaries.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        query = "SELECT * FROM trips WHERE 1=1"
        params: list[Any] = []

        if vin:
            query += " AND vin = ?"
            params.append(vin)

        if category:
            query += " AND category = ?"
            params.append(category)

        if start_date:
            query += " AND start_time >= ?"
            params.append(int(start_date.timestamp() * 1000))

        if end_date:
            query += " AND start_time <= ?"
            params.append(int(end_date.timestamp() * 1000))

        query += " ORDER BY start_time DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor.execute(query, params)
        rows = cursor.fetchall()

        return [dict(row) for row in rows]

    def get_trips_without_addresses(
        self,
        vin: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Get trips that are missing start or end addresses.

        Args:
            vin: Filter by vehicle (optional).
            limit: Maximum number of trips to return.

        Returns:
            List of trip dictionaries with GPS coordinates but no addresses.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        query = """
            SELECT * FROM trips 
            WHERE (
                (start_address IS NULL OR start_address = '') 
                AND start_lat IS NOT NULL 
                AND start_lon IS NOT NULL
            ) OR (
                (end_address IS NULL OR end_address = '') 
                AND end_lat IS NOT NULL 
                AND end_lon IS NOT NULL
            )
        """
        params: list[Any] = []

        if vin:
            query += " AND vin = ?"
            params.append(vin)

        query += " ORDER BY start_time DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()

        return [dict(row) for row in rows]

    def update_trip(
        self,
        trip_db_id: int,
        purpose: str | None = None,
        category: str | None = None,
        notes: str | None = None,
        start_address: str | None = None,
        end_address: str | None = None,
    ) -> bool:
        """Update user-editable fields for a trip.

        Args:
            trip_db_id: Database row ID of the trip.
            purpose: Trip purpose (e.g., "work", "private").
            category: Trip category.
            notes: Additional notes.
            start_address: Human-readable start address.
            end_address: Human-readable end address.

        Returns:
            True if update was successful.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        updates = []
        params: list[Any] = []

        if purpose is not None:
            updates.append("purpose = ?")
            params.append(purpose)

        if category is not None:
            updates.append("category = ?")
            params.append(category)

        if notes is not None:
            updates.append("notes = ?")
            params.append(notes)

        if start_address is not None:
            updates.append("start_address = ?")
            params.append(start_address)

        if end_address is not None:
            updates.append("end_address = ?")
            params.append(end_address)

        if not updates:
            return False

        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(trip_db_id)

        query = f"UPDATE trips SET {', '.join(updates)} WHERE id = ?"
        cursor.execute(query, params)
        conn.commit()

        return cursor.rowcount > 0

    def get_statistics(
        self,
        vin: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> dict[str, Any]:
        """Get aggregated statistics for trips.

        Args:
            vin: Filter by vehicle (optional).
            start_date: Filter trips starting after this date (optional).
            end_date: Filter trips starting before this date (optional).

        Returns:
            Dictionary with statistics.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        query = """
            SELECT
                COUNT(*) as total_trips,
                COALESCE(SUM(distance_km), 0) as total_distance_km,
                COALESCE(SUM(duration_min), 0) as total_duration_min,
                COALESCE(AVG(consumption_kwh), 0) as avg_consumption_kwh,
                COALESCE(SUM(regeneration_wh), 0) as total_regeneration_wh,
                COALESCE(AVG(avg_speed_kmh), 0) as avg_speed_kmh,
                MIN(start_time) as first_trip_time,
                MAX(start_time) as last_trip_time
            FROM trips WHERE 1=1
        """
        params: list[Any] = []

        if vin:
            query += " AND vin = ?"
            params.append(vin)

        if start_date:
            query += " AND start_time >= ?"
            params.append(int(start_date.timestamp() * 1000))

        if end_date:
            query += " AND start_time <= ?"
            params.append(int(end_date.timestamp() * 1000))

        cursor.execute(query, params)
        row = cursor.fetchone()

        if row:
            return dict(row)

        return {
            "total_trips": 0,
            "total_distance_km": 0,
            "total_duration_min": 0,
            "avg_consumption_kwh": 0,
            "total_regeneration_wh": 0,
            "avg_speed_kmh": 0,
            "first_trip_time": None,
            "last_trip_time": None,
        }

    def get_uncategorized_count(self, vin: str | None = None) -> int:
        """Get count of trips that haven't been categorized.

        Args:
            vin: Filter by vehicle (optional).

        Returns:
            Number of uncategorized trips.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        if vin:
            cursor.execute(
                "SELECT COUNT(*) FROM trips WHERE vin = ? AND category = 'uncategorized'",
                (vin,),
            )
        else:
            cursor.execute(
                "SELECT COUNT(*) FROM trips WHERE category = 'uncategorized'"
            )

        row = cursor.fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        """Close database connection."""
        if self._connection:
            self._connection.close()
            self._connection = None
