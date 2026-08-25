from abc import ABC, abstractmethod
from typing import Optional

from backend.supervisor.state import CallState


class SlotAlreadyBookedError(Exception):
    pass


class CallRepository(ABC):
    @abstractmethod
    def upsert(self, state: CallState, outcome_override: Optional[str] = None) -> None: ...

    @abstractmethod
    def get(self, call_id: str) -> Optional[dict]: ...

    @abstractmethod
    def list(self, *, with_outcome_only: bool = False, reviewed: Optional[bool] = None) -> list[dict]: ...


class SlotRepository(ABC):
    @abstractmethod
    def check_availability(
        self,
        date: str,
        window: str,
        area: str,
        exact_time: Optional[str] = None,
        exclude_ids: Optional[list[int]] = None,
    ) -> Optional[dict]: ...

    @abstractmethod
    def suggest_alternatives(self, date: str, area: str, exclude_ids: list[int]) -> list[dict]: ...

    @abstractmethod
    def book(self, slot_id: int) -> int: ...

    @abstractmethod
    def seed(self, areas: list[str], business_days: int) -> None: ...


class TraceRepository(ABC):
    @abstractmethod
    def record_event(self, call_id: str, event_type: str, node: Optional[str] = None, **payload) -> None: ...

    @abstractmethod
    def get_trace(self, call_id: str) -> list[dict]: ...


class EvalRepository(ABC):
    @abstractmethod
    def add_error_flags(self, call_id: str, flags: list[dict], eval_run_label: str) -> None: ...

    @abstractmethod
    def add_taxonomy_suggestions(self, suggestions: list[dict], eval_run_label: str) -> None: ...

    @abstractmethod
    def update_suggestion_status(self, suggestion_id: int, status: str) -> None: ...

    @abstractmethod
    def tag_eval_run(self, call_id: str, eval_run_label: str, scenario_id: Optional[str] = None) -> None: ...

    @abstractmethod
    def compute_error_rates(self, eval_run_label: str) -> dict[str, float]: ...

    @abstractmethod
    def compute_error_rates_all(self) -> dict[str, float]: ...

    @abstractmethod
    def list_taxonomy_suggestions(
        self, eval_run_label: Optional[str], status: Optional[str]
    ) -> list[dict]: ...

    @abstractmethod
    def get_error_flags(self, call_id: str) -> list[dict]: ...

    @abstractmethod
    def call_ids_already_evaluated(self) -> set[str]: ...


class DevRepository(ABC):
    @abstractmethod
    def list_tables(self) -> list[str]: ...

    @abstractmethod
    def get_table(self, table: str, *, limit: int = 100, offset: int = 0) -> dict: ...


class AnnotationRepository(ABC):
    @abstractmethod
    def save_review(
        self,
        call_id: str,
        annotator: str,
        error_class_ids: list[str],
        uncategorized_notes: list[str],
        overall_note: str,
        is_gold: bool,
    ) -> None: ...

    @abstractmethod
    def get_review(self, call_id: str) -> Optional[dict]: ...

    @abstractmethod
    def list_unreviewed(self) -> list[dict]: ...
