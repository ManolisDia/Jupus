import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from backend import dispatcher
from backend.config import settings
from backend.db.repositories import Repositories, get_repositories
from eval.insights_agent import run_deterministic_pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
REPOS = get_repositories(settings)

ADMIN_DIR = Path(__file__).resolve().parents[1] / "admin"


def get_repos() -> Repositories:
    return REPOS

# Local-only prototype: client/index.html is opened directly as a file:// page
# (no bundler/dev server per docs/DECISIONS.md), so its origin is "null" —
# CORS must be wide open for it to reach this backend at all. Once deployed
# (Phase 9), PUBLIC_CLIENT_ORIGIN locks this down to the real Firebase origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.public_client_origin] if settings.public_client_origin else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def verify_access_token(access_token: Optional[str] = None) -> None:
    # No-op (always passes) when settings.jupus_access_token is unset — local
    # dev is unaffected. When set, requires an exact match (Phase 9 gate).
    if settings.jupus_access_token and access_token != settings.jupus_access_token:
        raise HTTPException(status_code=401, detail="invalid or missing access token")


@app.middleware("http")
async def admin_access_gate(request: Request, call_next):
    # Gates /admin and /admin/annotate behind the same shared-secret query
    # param as /session and /bridge (Phase 9, Decision 3) — a no-op when
    # jupus_access_token is unset. Scoped to a single middleware rather than
    # a per-route dependency since StaticFiles serves many files under the
    # /admin mount.
    if settings.jupus_access_token and request.url.path.startswith("/admin"):
        access_token = request.query_params.get("access_token")
        if access_token != settings.jupus_access_token:
            return PlainTextResponse("invalid or missing access token", status_code=401)
    return await call_next(request)

REALTIME_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
REALTIME_MODEL = "gpt-realtime-2.1"
REALTIME_VOICE = "marin"


class SessionRequest(BaseModel):
    call_id: str


@app.post("/session", dependencies=[Depends(verify_access_token)])
async def create_session(request: SessionRequest):
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                REALTIME_CLIENT_SECRETS_URL,
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "session": {
                        "type": "realtime",
                        "model": REALTIME_MODEL,
                        "audio": {"output": {"voice": REALTIME_VOICE}},
                    }
                },
            )
        except httpx.HTTPError:
            return JSONResponse(
                status_code=502, content={"error": "failed to reach OpenAI Realtime API"}
            )

    if response.status_code >= 400:
        return JSONResponse(
            status_code=502,
            content={"error": "OpenAI Realtime session creation failed"},
        )

    try:
        data = response.json()
        return {
            "client_secret": data["value"],
            "session_id": data["session"]["id"],
            "expires_at": str(data["expires_at"]),
        }
    except (ValueError, KeyError):
        return JSONResponse(
            status_code=502,
            content={"error": "unexpected response shape from OpenAI Realtime API"},
        )


class BridgeMessage(BaseModel):
    type: str
    tool_call_id: Optional[str] = None
    reason: Optional[str] = None
    last_caller_utterance: Optional[str] = None


@app.websocket("/bridge")
async def bridge(
    websocket: WebSocket,
    call_id: str,
    access_token: Optional[str] = None,
    repos: Repositories = Depends(get_repos),
):
    if settings.jupus_access_token and access_token != settings.jupus_access_token:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    dispatcher.CONNECTIONS[call_id] = websocket
    while True:
        try:
            raw = await websocket.receive_text()
            msg = BridgeMessage.model_validate_json(raw)
        except (json.JSONDecodeError, ValidationError):
            logger.warning("malformed /bridge message call_id=%s: %r", call_id, raw)
            continue
        except WebSocketDisconnect:
            await dispatcher.mark_call_abandoned(repos, call_id)
            break

        await dispatcher.on_bridge_message(repos, call_id, msg.model_dump(exclude_none=True))


TRACE_STREAM_POLL_SECONDS = 0.4


@app.websocket("/admin/trace/{call_id}")
async def admin_trace_stream(websocket: WebSocket, call_id: str, repos: Repositories = Depends(get_repos)):
    # Read-only spectator feed for the Phase 7 "live supervisor mind"
    # stretch (admin/graph.html). Trace events read exclusively through
    # TraceRepository (rule #9) — a rendering layer on top of
    # instrumentation that already exists (traced_call/call_claude_tool,
    # rule #8), not a new logging path. The call_state snapshot is read
    # straight from dispatcher.CALL_STATES (in-memory, no repo involved —
    # it's the same live-process state a *live* call already holds, not a
    # database row), which is what lets the graph page show the field-by-
    # field capture / slot-proposal sub-state that lives inside the
    # "capture"/"booking" nodes but isn't its own trace event. Polls rather
    # than pushes: simplest thing that works at this scale, and it never
    # sends anything back into the call path — it has zero ability to
    # affect a live call or add latency/risk to it.
    await websocket.accept()
    sent = 0
    last_state_json: Optional[str] = None
    try:
        while True:
            events = repos.trace.get_trace(call_id)
            if len(events) > sent:
                await websocket.send_json({"type": "trace_events", "events": events[sent:]})
                sent = len(events)

            state = dispatcher.CALL_STATES.get(call_id)
            if state is not None:
                snapshot = dispatcher.call_state_snapshot(state)
                snapshot_json = json.dumps(snapshot, sort_keys=True)
                if snapshot_json != last_state_json:
                    await websocket.send_json(snapshot)
                    last_state_json = snapshot_json

            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=TRACE_STREAM_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass


# ---------------------------------------------------------------------------
# Admin panel — base scaffold from docs/phases/phase-6a-observability.md,
# extended per docs/phases/phase-6b-error-taxonomy.md (error-class badges/
# evidence, error_rates) and docs/phases/phase-6c-benevolent-dictator.md
# (reviewed flag/human review, taxonomy-suggestions, BD annotation routes).
# No auth (local-only prototype).
# ---------------------------------------------------------------------------


def _error_badges(repos: Repositories, call_id: str) -> list[str]:
    if repos.evals is None:
        return []
    flags = repos.evals.get_error_flags(call_id)
    return sorted({f["error_class_id"] for f in flags})


@app.get("/api/calls")
async def api_calls_list(repos: Repositories = Depends(get_repos)):
    rows = repos.calls.list()
    reviewed_ids = set()
    if repos.annotations is not None:
        for row in rows:
            if repos.annotations.get_review(row["call_id"]) is not None:
                reviewed_ids.add(row["call_id"])
    return [
        {
            "call_id": r["call_id"],
            "started_at": r["started_at"],
            "practice_area": r["practice_area"],
            "outcome": r["outcome"],
            "escalation_reason": r["escalation_reason"],
            "booking_slot_id": r["booking_slot_id"],
            "error_classes": _error_badges(repos, r["call_id"]),
            "reviewed": r["call_id"] in reviewed_ids,
        }
        for r in rows
    ]


@app.get("/api/calls/unreviewed")
async def api_calls_unreviewed(repos: Repositories = Depends(get_repos)):
    # Registered before "/api/calls/{call_id}" — a path-parameter route
    # registered first would otherwise greedily match "unreviewed" as a
    # call_id (FastAPI/Starlette match routes in registration order).
    return repos.annotations.list_unreviewed()


@app.get("/api/calls/{call_id}")
async def api_call_detail(call_id: str, repos: Repositories = Depends(get_repos)):
    row = repos.calls.get(call_id)
    if row is None:
        raise HTTPException(status_code=404, detail="call not found")
    transcript = json.loads(row["transcript_json"]) if row.get("transcript_json") else []
    error_flags = repos.evals.get_error_flags(call_id) if repos.evals is not None else []
    human_review = repos.annotations.get_review(call_id) if repos.annotations is not None else None
    return {
        "call_id": row["call_id"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "practice_area": row["practice_area"],
        "outcome": row["outcome"],
        "escalation_reason": row["escalation_reason"],
        "caller_name": row["caller_name"],
        "caller_email": row["caller_email"],
        "caller_phone": row["caller_phone"],
        "booking_slot_id": row["booking_slot_id"],
        "transcript": transcript,
        "call_error_flags": error_flags,
        "human_review": human_review,
    }


@app.get("/api/calls/{call_id}/trace")
async def api_call_trace(call_id: str, repos: Repositories = Depends(get_repos)):
    return repos.trace.get_trace(call_id)


@app.get("/api/eval/summary")
async def api_eval_summary(repos: Repositories = Depends(get_repos), label: str | None = None):
    calls = repos.calls.list()
    summary = run_deterministic_pass(repos, calls)
    if repos.evals is not None:
        summary["error_rates"] = (
            repos.evals.compute_error_rates(label) if label else repos.evals.compute_error_rates_all()
        )
    return summary


@app.get("/api/eval/error-classes")
async def api_error_classes():
    from eval.error_classes import get_active_error_classes

    return get_active_error_classes()


@app.get("/api/eval/taxonomy-suggestions")
async def api_taxonomy_suggestions(
    repos: Repositories = Depends(get_repos), label: str | None = None, status: str | None = None
):
    return repos.evals.list_taxonomy_suggestions(label, status)


@app.post("/api/eval/taxonomy-suggestions/{suggestion_id}/approve")
async def api_approve_taxonomy_suggestion(suggestion_id: int, repos: Repositories = Depends(get_repos)):
    repos.evals.update_suggestion_status(suggestion_id, "approved")
    return {"id": suggestion_id, "status": "approved"}


@app.post("/api/eval/taxonomy-suggestions/{suggestion_id}/reject")
async def api_reject_taxonomy_suggestion(suggestion_id: int, repos: Repositories = Depends(get_repos)):
    repos.evals.update_suggestion_status(suggestion_id, "rejected")
    return {"id": suggestion_id, "status": "rejected"}


@app.get("/api/eval/compare")
async def api_compare_runs(baseline: str, candidate: str, repos: Repositories = Depends(get_repos)):
    from eval.compare_runs import build_comparison

    return build_comparison(repos, baseline, candidate)


@app.get("/api/calls/{call_id}/review")
async def api_get_review(call_id: str, repos: Repositories = Depends(get_repos)):
    review = repos.annotations.get_review(call_id)
    if review is None:
        raise HTTPException(status_code=404, detail="call has not been reviewed yet")
    return review


class ReviewRequest(BaseModel):
    error_class_ids: list[str] = []
    uncategorized_notes: list[str] = []
    overall_note: str = ""
    is_gold: bool = False


@app.post("/api/calls/{call_id}/review")
async def api_post_review(call_id: str, request: ReviewRequest, repos: Repositories = Depends(get_repos)):
    repos.annotations.save_review(
        call_id,
        annotator=settings.annotator_name,
        error_class_ids=request.error_class_ids,
        uncategorized_notes=request.uncategorized_notes,
        overall_note=request.overall_note,
        is_gold=request.is_gold,
    )
    return repos.annotations.get_review(call_id)


@app.get("/admin/annotate")
async def admin_annotate_page():
    # StaticFiles(html=True) below only auto-serves "index.html" for a bare
    # directory path — "/admin/annotate" (no .html) needs its own route to
    # reach admin/annotate.html, the Benevolent Dictator's dedicated page.
    return FileResponse(ADMIN_DIR / "annotate.html")


# Mounted last so it never shadows the /api/* or /admin/annotate routes
# above. html=True serves admin/index.html for both "/admin" and "/admin/".
app.mount("/admin", StaticFiles(directory=ADMIN_DIR, html=True), name="admin")
