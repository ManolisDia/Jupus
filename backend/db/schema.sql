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

CREATE TABLE eval_flags (
    call_id TEXT PRIMARY KEY REFERENCES calls(call_id),
    flagged INTEGER,
    flag_reason TEXT,
    evaluated_at TEXT
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
CREATE INDEX idx_trace_events_call_seq ON trace_events(call_id, seq);
