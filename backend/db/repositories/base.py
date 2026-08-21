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
    def check_availability(self, date: str, window: str, area: str) -> Optional[dict]: ...

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
