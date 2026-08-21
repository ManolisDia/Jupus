import json
import logging

import httpx
from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from backend import dispatcher
from backend.config import settings
from backend.db.repositories import Repositories, get_repositories

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
REPOS = get_repositories(settings)


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
    tool_call_id: str
    reason: str
    last_caller_utterance: str


@app.websocket("/bridge")
async def bridge(websocket: WebSocket, call_id: str, repos: Repositories = Depends(get_repos)):
    await websocket.accept()
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

        reply = await dispatcher.on_ask_supervisor(
            repos, call_id, msg.tool_call_id, msg.reason, msg.last_caller_utterance
        )
        await websocket.send_json(
            {"type": "supervisor_result", "tool_call_id": msg.tool_call_id, "reply": reply}
        )
