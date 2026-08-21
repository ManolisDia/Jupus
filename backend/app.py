import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.config import settings

app = FastAPI()

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

    data = response.json()
    return {
        "client_secret": data["value"],
        "session_id": data["session"]["id"],
        "expires_at": str(data["expires_at"]),
    }
