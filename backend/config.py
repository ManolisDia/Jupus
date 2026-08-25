from typing import Literal, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    openai_api_key: str
    anthropic_api_key: str
    port: int = Field(default=8000, validation_alias="JUPUS_PORT")
    db_path: str = Field(
        default="backend/db/calendar.db", validation_alias="JUPUS_DB_PATH"
    )
    db_backend: Literal["sqlite", "postgres"] = "sqlite"
    database_url: Optional[str] = None
    # Phase 6c — the Benevolent Dictator's fixed identity label (single-user
    # local tool, not a real auth system; see docs/benevolent_dictator.md).
    annotator_name: str = "benevolent_dictator"

    # Phase 9 (hosted deployment) — both Optional, both None for local dev
    # (where the gate/CORS-tightening below simply don't apply).
    jupus_access_token: Optional[str] = None
    public_client_origin: Optional[str] = None  # e.g. "https://<project>.web.app"

    # Phase 14 (LiveKit transport) — required for a call to connect at all.
    # Optional so the backend still boots for admin/eval work without them.
    livekit_url: Optional[str] = None
    livekit_api_key: Optional[str] = None
    livekit_api_secret: Optional[str] = None

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()
