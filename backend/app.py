import json
import logging
from pathlib import Path
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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
# CORS must be wide open for it to reach this backend at all.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

REALTIME_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
REALTIME_MODEL = "gpt-realtime-2.1"
REALTIME_VOICE = "marin"


class SessionRequest(BaseModel):
    call_id: str


@app.post("/session")
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
async def bridge(websocket: WebSocket, call_id: str, repos: Repositories = Depends(get_repos)):
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
            dispatcher.mark_call_abandoned(repos, call_id)
            break

        await dispatcher.on_bridge_message(repos, call_id, msg.model_dump(exclude_none=True))


# ---------------------------------------------------------------------------
# Admin panel (base) — docs/phases/phase-6a-observability.md.
# No auth (local-only prototype). No error-class badges/reviewed flag/
# eval_flags key yet — those are additive in 6b/6c per that phase doc.
# ---------------------------------------------------------------------------


@app.get("/api/calls")
async def api_calls_list(repos: Repositories = Depends(get_repos)):
    rows = repos.calls.list()
    return [
        {
            "call_id": r["call_id"],
            "started_at": r["started_at"],
            "practice_area": r["practice_area"],
            "outcome": r["outcome"],
            "escalation_reason": r["escalation_reason"],
            "booking_slot_id": r["booking_slot_id"],
        }
        for r in rows
    ]


@app.get("/api/calls/{call_id}")
async def api_call_detail(call_id: str, repos: Repositories = Depends(get_repos)):
    row = repos.calls.get(call_id)
    if row is None:
        raise HTTPException(status_code=404, detail="call not found")
    transcript = json.loads(row["transcript_json"]) if row.get("transcript_json") else []
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
    }


@app.get("/api/calls/{call_id}/trace")
async def api_call_trace(call_id: str, repos: Repositories = Depends(get_repos)):
    return repos.trace.get_trace(call_id)


@app.get("/api/eval/summary")
async def api_eval_summary(repos: Repositories = Depends(get_repos)):
    calls = repos.calls.list()
    return run_deterministic_pass(repos, calls)


# Mounted last so it never shadows the /api/* routes above. html=True serves
# admin/index.html for both "/admin" and "/admin/".
app.mount("/admin", StaticFiles(directory=ADMIN_DIR, html=True), name="admin")
