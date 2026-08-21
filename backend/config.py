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

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()
