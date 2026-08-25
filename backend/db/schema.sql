CREATE TABLE slots (
    id INTEGER PRIMARY KEY,
    area TEXT NOT NULL,
    start_time TEXT NOT NULL,
    is_booked INTEGER DEFAULT 0
);

CREATE TABLE calls (
    call_id TEXT PRIMARY KEY,
    started_at TEXT,
    ended_at TEXT,
    practice_area TEXT,
    outcome TEXT,
    escalation_reason TEXT,
    caller_name TEXT,
    caller_email TEXT,
    caller_phone TEXT,
    booking_slot_id INTEGER,
    transcript_json TEXT
);

-- Superseded by call_error_flags/eval_runs below (Phase 6b) — see
-- docs/phases/phase-2-supervisor-skeleton.md's note on why a placeholder
-- table existed here before the real eval schema was designed.
CREATE TABLE call_error_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT REFERENCES calls(call_id),
    error_class_id TEXT NOT NULL,     -- references eval/error_classes.py ids,
                                       -- not FK-enforced (taxonomy lives in code)
    confidence REAL,
    evidence TEXT,
    eval_run_label TEXT NOT NULL,
    evaluated_at TEXT
);

CREATE TABLE eval_runs (
    call_id TEXT REFERENCES calls(call_id),
    eval_run_label TEXT NOT NULL,
    scenario_id TEXT,
    created_at TEXT,
    PRIMARY KEY (call_id, eval_run_label)
);

-- Phase 6c — taxonomy critique + Benevolent Dictator annotation tables.
CREATE TABLE taxonomy_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_run_label TEXT NOT NULL,
    call_id TEXT REFERENCES calls(call_id),
    suggestion_type TEXT NOT NULL,
    related_error_class_id TEXT,
    suggested_name TEXT,
    rationale TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    evaluated_at TEXT
);

CREATE TABLE call_reviews (
    call_id TEXT PRIMARY KEY REFERENCES calls(call_id),
    annotator TEXT NOT NULL,
    is_gold INTEGER DEFAULT 0,
    overall_note TEXT,
    reviewed_at TEXT
);

CREATE TABLE human_annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT REFERENCES calls(call_id),
    error_class_id TEXT,
    note TEXT,
    created_at TEXT
);

CREATE TABLE trace_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT REFERENCES calls(call_id),
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    node TEXT,
    payload_json TEXT NOT NULL
);
-- UNIQUE, not just an index: a duplicate (call_id, seq) means the seq_lock
-- in SQLiteTraceRepository.record_event was bypassed somehow — this turns
-- that into a loud, immediate failure instead of silently corrupting trace
-- ordering (docs/code-review-2026-08-24.md finding #2).
CREATE UNIQUE INDEX idx_trace_events_call_seq ON trace_events(call_id, seq);

-- The human-handoff record: what a person picking this call up off the
-- queue needs before they ring the caller back. One row per escalated call
-- — why they rang, whatever contact details we managed to confirm, and why
-- the agent gave up. docs/handoffs/{call_id}.md is the same data rendered
-- for a human to read; this table is the queryable copy.
-- reason_for_call/escalation_explanation are nullable because the
-- deterministic fallback path (an escalation triggered BY an LLM failure)
-- deliberately doesn't make another Claude call to fill them in — see
-- node_escalation in backend/supervisor/graph.py.
CREATE TABLE escalations (
    call_id TEXT PRIMARY KEY REFERENCES calls(call_id),
    escalated_at TEXT NOT NULL,
    escalation_reason TEXT,
    reason_for_call TEXT,
    escalation_explanation TEXT,
    practice_area TEXT,
    caller_name TEXT,
    caller_email TEXT,
    caller_phone TEXT
);
