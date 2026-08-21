import sqlite3
from typing import Optional

from backend.db.repositories.base import AnnotationRepository
from backend.utils import now_iso


class SQLiteAnnotationRepository(AnnotationRepository):
    """Backs call_reviews/human_annotations — the Benevolent Dictator's
    annotations (docs/benevolent_dictator.md, Phase 6c). The only file that
    knows these table names exist, per docs/architecture.md."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def save_review(
        self,
        call_id: str,
        annotator: str,
        error_class_ids: list[str],
        uncategorized_notes: list[str],
        overall_note: str,
        is_gold: bool,
    ) -> None:
        # Re-annotation is delete-then-insert, not accumulate (docs/phases/
        # phase-6c-benevolent-dictator.md) — exactly one active human review
        # per call at any time.
        self._conn.execute("DELETE FROM human_annotations WHERE call_id = ?", (call_id,))
        self._conn.execute(
            "INSERT INTO call_reviews (call_id, annotator, is_gold, overall_note, reviewed_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(call_id) DO UPDATE SET annotator = excluded.annotator, "
            "is_gold = excluded.is_gold, overall_note = excluded.overall_note, "
            "reviewed_at = excluded.reviewed_at",
            (call_id, annotator, int(is_gold), overall_note, now_iso()),
        )
        for error_class_id in error_class_ids:
            self._conn.execute(
                "INSERT INTO human_annotations (call_id, error_class_id, note, created_at) VALUES (?, ?, NULL, ?)",
                (call_id, error_class_id, now_iso()),
            )
        for note in uncategorized_notes:
            self._conn.execute(
                "INSERT INTO human_annotations (call_id, error_class_id, note, created_at) VALUES (?, NULL, ?, ?)",
                (call_id, note, now_iso()),
            )
        self._conn.commit()

    def get_review(self, call_id: str) -> Optional[dict]:
        review_cursor = self._conn.execute("SELECT * FROM call_reviews WHERE call_id = ?", (call_id,))
        review_row = review_cursor.fetchone()
        if review_row is None:
            return None
        columns = [d[0] for d in review_cursor.description]
        review = dict(zip(columns, review_row))

        annotations_cursor = self._conn.execute(
            "SELECT * FROM human_annotations WHERE call_id = ? ORDER BY id", (call_id,)
        )
        a_columns = [d[0] for d in annotations_cursor.description]
        review["annotations"] = [dict(zip(a_columns, row)) for row in annotations_cursor.fetchall()]
        return review

    def list_unreviewed(self) -> list[dict]:
        cursor = self._conn.execute(
            "SELECT calls.* FROM calls LEFT JOIN call_reviews ON calls.call_id = call_reviews.call_id "
            "WHERE call_reviews.call_id IS NULL ORDER BY calls.started_at ASC"
        )
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
