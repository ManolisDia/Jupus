# Architecture Diagrams

Four diagrams for presenting Jupus: the layered system architecture, the full call sequence (including the async/non-blocking supervisor dispatch), the LangGraph state machine, and the eval/Benevolent-Dictator data flow. Each corresponds to a section of `docs/PLAN.md` / `docs/architecture.md` — these are visual summaries, not a separate source of truth; if a diagram and a doc ever disagree, the doc wins.

---

## 1. System architecture

Layered shape from `docs/architecture.md`: transport → orchestration → domain/tools → data access, with the Realtime/Claude/SQLite boundaries.

```mermaid
flowchart TB
    subgraph Caller["Caller"]
        Mic["Browser client<br/>client/index.html"]
    end

    subgraph Realtime["OpenAI Realtime API"]
        RT["Realtime session<br/>STT + semantic VAD + dialogue + TTS<br/>sees exactly ONE tool: ask_supervisor"]
    end

    subgraph Backend["Backend (FastAPI)"]
        direction TB
        Transport["Transport<br/>app.py routes, /bridge WebSocket"]
        Orchestration["Orchestration<br/>dispatcher.py + graph.py (LangGraph)"]
        Domain["Domain / Tools<br/>tools.py — Claude calls + deterministic logic"]
        DataAccess["Data Access<br/>backend/db/repositories/ (ABCs)"]
        Transport --> Orchestration --> Domain --> DataAccess
    end

    subgraph Claude["Anthropic Claude"]
        LLM["Supervisor reasoning<br/>routing / extraction / booking / escalation / eval judge"]
    end

    subgraph Store["SQLite — swappable via the Repository pattern"]
        Tables["calls · slots · trace_events<br/>call_error_flags · taxonomy_suggestions<br/>call_reviews · human_annotations"]
    end

    subgraph AdminSide["Admin (Benevolent Dictator)"]
        AdminUI["admin/ + admin/annotate.js"]
    end

    Mic <-->|audio + data channel| RT
    RT <-->|ask_supervisor call / result| Transport
    Domain <-->|forced tool-calling| LLM
    DataAccess --> Tables
    AdminUI <-->|REST| Transport
```

---

## 2. Call sequence

The full round trip from `docs/PLAN.md`, including the non-blocking dispatch that lets the caller keep talking while the supervisor works (Phase 5).

```mermaid
sequenceDiagram
    actor Caller
    participant Browser as Browser Client
    participant RT as OpenAI Realtime
    participant Bridge as Backend /bridge
    participant Graph as LangGraph Supervisor
    participant Claude
    participant DB as Repositories (SQLite)

    Browser->>Bridge: POST /session {call_id}
    Bridge->>RT: create session (server-held API key)
    RT-->>Bridge: ephemeral client_secret
    Bridge-->>Browser: client_secret
    Browser->>RT: WebRTC offer (mic track + data channel)
    RT-->>Browser: WebRTC answer
    Browser->>RT: session.update (semantic_vad, ask_supervisor tool)
    Browser->>Bridge: open WS /bridge?call_id

    Caller->>RT: speaks
    RT->>RT: STT + semantic VAD (end of turn)
    RT->>Browser: ask_supervisor(reason, utterance)
    Browser->>Bridge: forward over WebSocket
    Bridge->>Bridge: asyncio.create_task (never blocks)
    Bridge-->>Browser: (returns immediately)

    par caller keeps talking
        Caller->>RT: next utterance — handled independently
    and supervisor works in the background
        Bridge->>Graph: GRAPH.invoke(state, config)
        Graph->>Claude: scoped tool call (traced + retried)
        Claude-->>Graph: structured result
        Graph->>DB: repos.calls.upsert / repos.trace.record_event
        Graph-->>Bridge: pending_reply
    end

    alt caller not speaking
        Bridge->>Browser: deliver reply now
    else caller mid-speech
        Bridge->>Bridge: queue in DEFERRED
        Note over Bridge: delivered on next speech_stopped,<br/>dropped if the stage went stale first
    end

    Browser->>RT: function_call_output + response.create
    RT->>Caller: speaks the reply
```

---

## 3. LangGraph state machine

Nodes, edges, and every escalation trigger from `docs/PLAN.md` and Phase 5.

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

    Escalation -->|handoff note written| EscalatedEnd(("ended: escalated"))
```
`explicit_request` (a deterministic keyword match, checked before the current stage's node runs) and `system_error` (3 consecutive upstream API failures, any node) can fire from anywhere — the dashed `any stage, any time` node represents that, it isn't a real graph state.

---

## 4. Eval / Benevolent Dictator data flow

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
