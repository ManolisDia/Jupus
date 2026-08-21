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

    def check_availability(self, date: str, window: str, area: str) -> Optional[dict]:
        cursor = self._conn.execute(
            "SELECT id, area, start_time, is_booked FROM slots "
            "WHERE area = ? AND start_time LIKE ? AND is_booked = 0 ORDER BY start_time LIMIT 1",
            (area, f"{date}T{window}%"),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [d[0] for d in cursor.description]
        return dict(zip(columns, row))

    def suggest_alternatives(self, date: str, area: str, exclude_ids: list[int]) -> list[dict]:
        placeholders = ",".join("?" for _ in exclude_ids) if exclude_ids else "NULL"
        cursor = self._conn.execute(
            f"SELECT id, area, start_time, is_booked FROM slots "
            f"WHERE area = ? AND start_time LIKE ? AND is_booked = 0 AND id NOT IN ({placeholders}) "
            f"ORDER BY start_time",
            (area, f"{date}%", *exclude_ids),
        )
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def book(self, slot_id: int) -> int:
        cursor = self._conn.execute("SELECT is_booked FROM slots WHERE id = ?", (slot_id,))
        row = cursor.fetchone()
        if row is None or row[0] == 1:
            raise SlotAlreadyBookedError(f"slot {slot_id} is not available")
        self._conn.execute("UPDATE slots SET is_booked = 1 WHERE id = ?", (slot_id,))
        self._conn.commit()
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
