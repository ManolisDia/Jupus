# Architecture Diagrams

Three diagrams for presenting Jupus: the layered system architecture, the LangGraph state machine, and the eval/Benevolent-Dictator data flow. Each corresponds to a section of `docs/PLAN.md` / `docs/architecture.md` — these are visual summaries, not a separate source of truth; if a diagram and a doc ever disagree, the doc wins.

> A fourth diagram, a call sequence, was removed after Phase 14. It described the hand-rolled `/bridge` WebSocket transport — `POST /session`, browser-side `session.update`, fire-and-forget `asyncio.create_task`, and the `SPEAKING`/`DEFERRED` delivery queue — all of which the LiveKit migration deleted (`docs/phases/phase-14-livekit-transport.md`, Decision 5). Rather than leave a diagram that would send a reader looking for a WebSocket that no longer exists, it's gone; `docs/reference/life-of-a-call.md` describes the current round trip in prose.

---

## 1. System architecture

Layered shape from `docs/architecture.md`: transport → orchestration → domain/tools → data access, with the LiveKit/Realtime/Claude/SQLite boundaries.

```mermaid
flowchart TB
    subgraph Caller["Caller"]
        Mic["Browser client<br/>client/index.html + livekit-transport.js"]
    end

    subgraph LK["LiveKit Cloud"]
        Room["LiveKit room<br/>WebRTC media + data channel"]
    end

    subgraph Backend["Backend — ONE FastAPI process"]
        direction TB
        Worker["LiveKit agent worker (in-process)<br/>backend/transport/livekit_agent.py<br/>hosts the Realtime session, schedules fillers"]
        Routes["HTTP / WS routes<br/>app.py — POST /livekit-token,<br/>WS /admin/trace/:call_id, /api/*"]
        Orchestration["Orchestration<br/>dispatcher.py + graph.py (LangGraph)"]
        Domain["Domain / Tools<br/>tools.py — Claude calls + deterministic logic"]
        DataAccess["Data Access<br/>backend/db/repositories/ (ABCs)"]
        Worker -->|ask_supervisor| Orchestration
        Orchestration --> Domain --> DataAccess
    end

    subgraph Realtime["OpenAI Realtime API"]
        RT["Realtime session<br/>STT + semantic VAD + dialogue + TTS<br/>sees exactly ONE tool: ask_supervisor"]
    end

    subgraph Claude["Anthropic Claude"]
        LLM["Supervisor reasoning<br/>routing / extraction / research /<br/>booking / escalation / eval judge"]
    end

    subgraph Store["SQLite — swappable via the Repository pattern"]
        Tables["calls · slots · trace_events · eval_runs · escalations<br/>call_error_flags · taxonomy_suggestions<br/>call_reviews · human_annotations"]
    end

    subgraph AdminSide["Admin (Benevolent Dictator)"]
        AdminUI["admin/ + admin/annotate.js"]
    end

    Mic <-->|WebRTC audio| Room
    Mic -.->|POST /livekit-token| Routes
    Room <-->|audio track + call_state data channel| Worker
    Worker <-->|speech in / speech out| RT
    Domain <-->|forced tool-calling| LLM
    DataAccess --> Tables
    AdminUI <-->|REST + live trace stream| Routes
```

The agent worker runs **inside the backend process**, not as a separate service — that is what lets it read in-memory call state directly and keeps the live admin view working, at the cost of one process doing real-time media work alongside serving HTTP. See the README's "Known limits" and the one-worker rule in `docs/reference/operations.md`.

---

## 2. LangGraph state machine

Nodes, edges, and every escalation trigger from `docs/PLAN.md` and Phase 5. Drawn at *stage* level, matching `CallState.stage` — the `capture` stage splits internally into `capture_fast`/`capture_confirm` nodes, and `research` into `research_gather`/`research_deliver`.

```mermaid
flowchart TD
    Start(("start")) --> Greeting["greeting"]
    Greeting -->|caller states intent| Routing["routing"]

    Routing -->|unclear, 1st attempt| Routing
    Routing -->|area classified| Capture["capture"]
    Routing -->|"unclear ×2 → unable_to_classify"| Escalation[["escalation"]]
    Routing -->|"multiple_areas → out_of_scope_multi_area"| Escalation

    Capture -->|"low/medium confidence → reprompt"| Capture
    Capture -->|all fields confirmed| Research["research<br/>(Phase 8)"]
    Capture -->|"3 failed attempts → capture_failed"| Escalation

    Research -->|"follow-up + background statute search"| Research
    Research -->|"citation delivered (or none found)"| Booking["booking"]

    Booking -->|"slot taken → alternative offered"| Booking
    Booking -->|caller accepts| BookedEnd(("ended: booked"))
    Booking -->|"no alternatives / 2 declines → no_acceptable_slot"| Escalation

    AnyStage{{"any stage, any time"}} -.->|explicit_request| Escalation
    AnyStage -.->|"system_error ×3 consecutive"| Escalation

    Escalation -->|"escalations row + handoff note written"| EscalatedEnd(("ended: escalated"))
```
`explicit_request` (a deterministic keyword match in `dispatcher.py`, checked before the current stage's node runs) and `system_error` (3 consecutive upstream API failures, any node) can fire from anywhere — the dashed `any stage, any time` node represents that, it isn't a real graph state.

---

## 3. Eval / Benevolent Dictator data flow

How a call becomes a classified, calibrated, regression-testable data point — `docs/error_taxonomy.md`, `docs/benevolent_dictator.md`, Phases 6a–6c.

```mermaid
flowchart TD
    Calls["calls + trace_events<br/>(live calls, or eval/replay_scenarios.py)"]

    Calls --> Judge["classify_call_errors<br/>LLM judge — Phase 6b"]
    Judge --> Flags["call_error_flags<br/>tagged by eval_run_label"]

    Calls --> Annotate["/admin/annotate<br/>Benevolent Dictator — Phase 6c"]
    Annotate --> Reviews["call_reviews + human_annotations"]

    Flags --> Critique["propose_taxonomy_updates<br/>weighs BD disagreement heavily"]
    Reviews --> Critique
    Critique --> Suggestions["taxonomy_suggestions<br/>status: pending"]
    Suggestions --> Approve{"BD approves in<br/>admin panel?"}
    Approve -- yes --> EditTaxonomy["hand-edit<br/>eval/error_classes.py"]
    Approve -- no --> Suggestions
    EditTaxonomy -.taxonomy evolves.-> Judge

    Flags --> Calibrate["eval/calibrate_judge.py"]
    Reviews --> Calibrate
    Calibrate --> CalScore["precision / recall per class<br/>judge vs BD ground truth"]

    Flags --> Rates["compute_error_rates<br/>per eval_run_label"]
    Rates --> Compare["eval/compare_runs.py<br/>--baseline vs --candidate"]
    Compare --> Regress{"any class regressed<br/>beyond threshold?"}
```
