import sqlite3
from typing import Optional

from backend.db.repositories.base import EvalRepository
from backend.utils import now_iso


class SQLiteEvalRepository(EvalRepository):
    """Backs eval_runs/call_error_flags (Phase 6b) and taxonomy_suggestions
    (Phase 6c). The only file that knows these table names exist, per
    docs/architecture.md."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # -- Phase 6b: error flags / eval runs -----------------------------------

    def tag_eval_run(self, call_id: str, eval_run_label: str, scenario_id: Optional[str] = None) -> None:
        self._conn.execute(
            "INSERT INTO eval_runs (call_id, eval_run_label, scenario_id, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(call_id, eval_run_label) DO UPDATE SET scenario_id = excluded.scenario_id",
            (call_id, eval_run_label, scenario_id, now_iso()),
        )
        self._conn.commit()

    def add_error_flags(self, call_id: str, flags: list[dict], eval_run_label: str) -> None:
        for flag in flags:
            self._conn.execute(
                "INSERT INTO call_error_flags (call_id, error_class_id, confidence, evidence, eval_run_label, evaluated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    call_id,
                    flag["error_class_id"],
                    flag.get("confidence"),
                    flag.get("evidence"),
                    eval_run_label,
                    now_iso(),
                ),
            )
        self._conn.commit()

    def get_error_flags(self, call_id: str) -> list[dict]:
        cursor = self._conn.execute(
            "SELECT * FROM call_error_flags WHERE call_id = ? ORDER BY id", (call_id,)
        )
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def compute_error_rates(self, eval_run_label: str) -> dict[str, float]:
        from eval.error_classes import get_active_error_classes  # local import: avoid a
        # backend -> eval import at module load time (eval depends on backend,
        # not the other way around, elsewhere in this codebase)

        total_calls_cursor = self._conn.execute(
            "SELECT COUNT(DISTINCT call_id) FROM eval_runs WHERE eval_run_label = ?", (eval_run_label,)
        )
        total_calls = total_calls_cursor.fetchone()[0]

        rates: dict[str, float] = {}
        for error_class in get_active_error_classes():
            class_id = error_class["id"]
            if total_calls == 0:
                rates[class_id] = 0.0
                continue
            flagged_cursor = self._conn.execute(
                "SELECT COUNT(DISTINCT call_id) FROM call_error_flags "
                "WHERE eval_run_label = ? AND error_class_id = ?",
                (eval_run_label, class_id),
            )
            flagged = flagged_cursor.fetchone()[0]
            rates[class_id] = flagged / total_calls
        return rates

    def compute_error_rates_all(self) -> dict[str, float]:
        """Same shape as compute_error_rates, but pooled across every
        eval_run_label ever recorded — used by GET /api/eval/summary when no
        specific ?label= is given (a reasonable "overall health" default for
        the admin panel rather than an arbitrary single label)."""
        from eval.error_classes import get_active_error_classes

        total_calls = self._conn.execute("SELECT COUNT(DISTINCT call_id) FROM eval_runs").fetchone()[0]

        rates: dict[str, float] = {}
        for error_class in get_active_error_classes():
            class_id = error_class["id"]
            if total_calls == 0:
                rates[class_id] = 0.0
                continue
            flagged = self._conn.execute(
                "SELECT COUNT(DISTINCT call_id) FROM call_error_flags WHERE error_class_id = ?",
                (class_id,),
            ).fetchone()[0]
            rates[class_id] = flagged / total_calls
        return rates

    def call_ids_already_evaluated(self) -> set[str]:
        cursor = self._conn.execute("SELECT DISTINCT call_id FROM eval_runs")
        return {row[0] for row in cursor.fetchall()}

    # -- Phase 6c: taxonomy suggestions --------------------------------------

    def add_taxonomy_suggestions(self, suggestions: list[dict], eval_run_label: str) -> None:
        for suggestion in suggestions:
            self._conn.execute(
                "INSERT INTO taxonomy_suggestions "
                "(eval_run_label, call_id, suggestion_type, related_error_class_id, suggested_name, rationale, status, evaluated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    eval_run_label,
                    suggestion.get("call_id"),
                    suggestion["suggestion_type"],
                    suggestion.get("related_error_class_id"),
                    suggestion.get("suggested_name"),
                    suggestion["rationale"],
                    now_iso(),
                ),
            )
        self._conn.commit()

    def update_suggestion_status(self, suggestion_id: int, status: str) -> None:
        self._conn.execute(
            "UPDATE taxonomy_suggestions SET status = ? WHERE id = ?", (status, suggestion_id)
        )
        self._conn.commit()

    def list_taxonomy_suggestions(
        self, eval_run_label: Optional[str], status: Optional[str]
    ) -> list[dict]:
        query = "SELECT * FROM taxonomy_suggestions WHERE 1=1"
        params: list = []
        if eval_run_label is not None:
            query += " AND eval_run_label = ?"
            params.append(eval_run_label)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY id"
        cursor = self._conn.execute(query, params)
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
