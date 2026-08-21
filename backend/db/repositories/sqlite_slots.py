import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from backend.db.repositories.base import SlotAlreadyBookedError, SlotRepository

BUSINESS_HOURS = [(9, 0), (17, 0)]
SLOT_MINUTES = 30
DAILY_START_HOUR = 9
DAILY_END_HOUR = 17
PRE_BOOKED_TIMES = {"10:00", "14:00"}


def _business_days(start: datetime, count: int) -> list[datetime]:
    days = []
    current = start
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def _daily_slot_times() -> list[str]:
    times = []
    hour, minute = DAILY_START_HOUR, 0
    while hour < DAILY_END_HOUR:
        times.append(f"{hour:02d}:{minute:02d}")
        minute += SLOT_MINUTES
        if minute >= 60:
            minute -= 60
            hour += 1
    return times


class SQLiteSlotRepository(SlotRepository):
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def check_availability(
        self,
        date: str,
        window: str,
        area: str,
        exact_time: Optional[str] = None,
        exclude_ids: Optional[list[int]] = None,
    ) -> Optional[dict]:
        query = (
            "SELECT id, area, start_time, is_booked FROM slots "
            "WHERE area = ? AND date(start_time) = ? AND is_booked = 0"
        )
        params: list = [area, date]
        if exact_time:
            query += " AND strftime('%H:%M', start_time) = ?"
            params.append(exact_time)
        elif window == "morning":
            query += " AND CAST(strftime('%H', start_time) AS INTEGER) < 12"
        elif window == "afternoon":
            query += " AND CAST(strftime('%H', start_time) AS INTEGER) >= 12"
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            query += f" AND id NOT IN ({placeholders})"
            params.extend(exclude_ids)
        query += " ORDER BY start_time LIMIT 1"
        cursor = self._conn.execute(query, params)
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [d[0] for d in cursor.description]
        return dict(zip(columns, row))

    def suggest_alternatives(self, date: str, area: str, exclude_ids: list[int]) -> list[dict]:
        # "id NOT IN (NULL)" (the old fallback for an empty exclude_ids) is
        # never true for any row in SQL's three-valued logic, which silently
        # matched zero slots — omit the clause entirely instead.
        query = (
            "SELECT id, area, start_time, is_booked FROM slots "
            "WHERE area = ? AND date(start_time) >= ? AND is_booked = 0"
        )
        params: list = [area, date]
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            query += f" AND id NOT IN ({placeholders})"
            params.extend(exclude_ids)
        query += " ORDER BY start_time LIMIT 3"
        cursor = self._conn.execute(query, params)
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def book(self, slot_id: int) -> int:
        # Atomic UPDATE-with-guard, not SELECT-then-UPDATE: makes the
        # check-then-act race (two near-simultaneous callers booking the
        # same slot) impossible rather than merely unlikely.
        cursor = self._conn.execute("UPDATE slots SET is_booked = 1 WHERE id = ? AND is_booked = 0", (slot_id,))
        self._conn.commit()
        if cursor.rowcount == 0:
            raise SlotAlreadyBookedError(f"slot {slot_id} is not available")
        return slot_id

    def seed(self, areas: list[str], business_days: int) -> None:
        self._conn.execute("DELETE FROM slots")
        days = _business_days(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0), business_days)
        times = _daily_slot_times()

        rows = []
        for day in days:
            for time_str in times:
                for area in areas:
                    start_time = f"{day.strftime('%Y-%m-%d')}T{time_str}:00"
                    rows.append((area, start_time))

        self._conn.executemany("INSERT INTO slots (area, start_time, is_booked) VALUES (?, ?, 0)", rows)

        first_day = days[0].strftime("%Y-%m-%d")
        for pre_booked_time in PRE_BOOKED_TIMES:
            self._conn.execute(
                "UPDATE slots SET is_booked = 1 WHERE start_time = ?",
                (f"{first_day}T{pre_booked_time}:00",),
            )
        self._conn.commit()
