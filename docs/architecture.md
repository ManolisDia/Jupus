# Architecture — Layers & the Repository Pattern

Read this before Phase 2 — it defines the shape every piece of persistence-touching code from that point on must follow. Retrofitting a repository layer after code is already calling `sqlite3` directly across five files is real, avoidable pain.

## Why this shape, not something else

Two explicit requirements drove this: (1) it should be straightforward to swap SQLite for a real hosted database later without touching business logic, and (2) the codebase should follow one strict, consistent pattern rather than ad-hoc data access wherever it's convenient. A repository layer solves both with one mechanism — it's the same pattern Femca already uses for Firestore (`BaseRepository`, all access routed through `src/domain/`), reused here rather than importing something heavier. Full Flutter-style Clean Architecture (entities/usecases/`fpdart` `Either`) is deliberately **not** used — that's a Dart/Flutter-ecosystem convention, and transplanting it into a Python backend would be ceremony without benefit. LangGraph's nodes and tools stay idiomatic plain functions, which is how that framework itself is designed to be used — the strict boundary is specifically the data layer, not everywhere uniformly.

## The four layers

```
Transport         backend/app.py (routes, WS handlers), client/, admin/
                   ─ thin, no business logic — mirrors the "views must be
                     dumb" rule from Foundermatcha's own Flutter side
                     │
Orchestration      backend/dispatcher.py, backend/supervisor/graph.py
                   ─ async dispatch + the LangGraph state machine;
                     coordinates, never talks to the DB except through
                     repositories
                     │
Domain / tools     backend/supervisor/tools.py
                   ─ business logic + Claude calls, idiomatic LangGraph
                     function style; persists through repositories, never
                     raw SQL
                     │
Data access        backend/db/repositories/
                   ─ the ONLY layer allowed to know SQL or table names
                     exist — interfaces (ABCs) + one SQLite implementation
```

## `backend/db/repositories/` — interfaces

```python
# backend/db/repositories/base.py
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
    def book(self, slot_id: int) -> int: ...       # raises SlotAlreadyBookedError
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
    def list_taxonomy_suggestions(self, eval_run_label: Optional[str], status: Optional[str]) -> list[dict]: ...

class AnnotationRepository(ABC):
    @abstractmethod
    def save_review(self, call_id: str, annotator: str, error_class_ids: list[str],
                     uncategorized_notes: list[str], overall_note: str, is_gold: bool) -> None: ...
    @abstractmethod
    def get_review(self, call_id: str) -> Optional[dict]: ...
    @abstractmethod
    def list_unreviewed(self) -> list[dict]: ...
```
One implementation file per interface: `backend/db/repositories/sqlite_calls.py` → `SQLiteCallRepository(CallRepository)`, and so on for `sqlite_slots.py`, `sqlite_trace.py`, `sqlite_eval.py`, `sqlite_annotations.py`. Each takes a `sqlite3.Connection` in `__init__` and is the *only* place in the codebase that writes SQL against its table(s).

## Wiring — the factory / DI seam

```python
# backend/db/repositories/__init__.py
@dataclass
class Repositories:
    calls: CallRepository
    slots: SlotRepository
    trace: TraceRepository
    evals: EvalRepository
    annotations: AnnotationRepository

def get_repositories(settings: Settings) -> Repositories:
    if settings.db_backend == "sqlite":
        conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        return Repositories(
            calls=SQLiteCallRepository(conn), slots=SQLiteSlotRepository(conn),
            trace=SQLiteTraceRepository(conn), evals=SQLiteEvalRepository(conn),
            annotations=SQLiteAnnotationRepository(conn),
        )
    raise NotImplementedError(
        f"db_backend={settings.db_backend!r} — implement Postgres*Repository "
        "classes against the same interfaces above and add a branch here. "
        "Nothing outside this file needs to change."
    )
```
Instantiated once at startup (`backend/app.py`), held as a module-level `REPOS = get_repositories(settings)`, exposed to FastAPI routes via a `Depends(get_repos)` wrapper so tests can substitute fakes. `backend/dispatcher.py`, `backend/supervisor/tools.py`, `eval/insights_agent.py`, and the admin routes all take a `Repositories` (or the single relevant repo) as a parameter/dependency — none of them import `sqlite3`.

### `backend/config.py` addition
```python
db_backend: Literal["sqlite", "postgres"] = "sqlite"
database_url: Optional[str] = None   # unused for sqlite; required if db_backend="postgres"
```
Add a commented-out `# DATABASE_URL=` line to `.env.example` for future use.

## What this changes in the phase docs

Every phase doc from Phase 2 onward shows `conn: sqlite3.Connection` threaded through function signatures (`upsert_call_record(conn, ...)`, `record_event(conn, ...)`, etc.) — written before this doc existed. Read every such signature as **illustrative of the operation, not the literal final signature**: the actual parameter is the relevant repository (`calls: CallRepository`, `trace: TraceRepository`, ...), and the SQL those functions describe belongs inside the matching `SQLiteXRepository` method, not inline in `dispatcher.py`/`tools.py`/`eval/insights_agent.py`. Where a phase doc says "extend `backend/db/repository.py`," read that as "extend the matching repository interface and its SQLite implementation" — the flat `repository.py` mentioned in early phase drafts is superseded by the `backend/db/repositories/` package described here.

## Testing benefit

Because business logic depends only on the ABCs, unit tests can inject simple in-memory fake repositories (plain Python classes backed by a dict, implementing the same interface) instead of spinning up a temp SQLite file — faster and simpler for pure logic tests. This is optional, not a mandate to rewrite every test already specified in the phase docs; a temp-SQLite-backed real repository works fine too and is what's assumed by default unless a phase doc says otherwise.

## Repo layout addition
```
backend/db/
  schema.sql
  seed_slots.py
  seed_demo_calls.py
  repositories/
    __init__.py           # Repositories dataclass + get_repositories()
    base.py                # the 5 ABCs above
    sqlite_calls.py
    sqlite_slots.py
    sqlite_trace.py
    sqlite_eval.py
    sqlite_annotations.py
  calendar.db              # gitignored
```
