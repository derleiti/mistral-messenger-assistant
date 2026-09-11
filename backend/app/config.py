from functools import lru_cache
import os
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .defaults import (
    DEFAULT_AGENT_DESCRIPTION,
    DEFAULT_AGENT_INSTRUCTIONS,
    DEFAULT_AGENT_MODEL,
    DEFAULT_AGENT_NAME,
)


def _default_database_path() -> str:
    """Use the persistent Docker volume when available, otherwise user data."""
    container_data = Path("/app/data")
    if container_data.is_dir() and os.access(container_data, os.W_OK):
        return str(container_data / "mistral_messenger.sqlite3")
    data_home = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return str(data_home / "mistral-messenger-assistant" / "mistral_messenger.sqlite3")


class Settings(BaseSettings):
    """Environment/bootstrap settings.

    Persistent setup values stored in SQLite override these values. Environment
    variables therefore remain useful for immutable/secret-based deployments,
    while a stock container can start with no credentials at all.
    """

    model_config = SettingsConfigDict(extra="ignore")

    database_path: str = Field(default_factory=_default_database_path)

    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    public_base_url: str = ""
    admin_token: str = ""
    owner_telegram_id: str = ""
    telegram_max_download_bytes: int = 20_000_000

    mistral_api_key: str = ""
    mistral_agent_id: str = ""
    mistral_agent_version: int | str | None = None
    mistral_api_base: str = "https://api.mistral.ai/v1"
    mistral_timeout_seconds: int = 210
    mistral_vision_model: str = "mistral-medium-latest"
    mistral_ocr_model: str = "mistral-ocr-latest"
    mistral_audio_model: str = "voxtral-mini-latest"
    media_context_max_chars: int = 160_000

    assistant_name: str = DEFAULT_AGENT_NAME
    agent_name: str = DEFAULT_AGENT_NAME
    agent_model: str = DEFAULT_AGENT_MODEL
    agent_description: str = DEFAULT_AGENT_DESCRIPTION
    agent_instructions: str = DEFAULT_AGENT_INSTRUCTIONS
    agent_enable_web_search: bool = True
    agent_enable_code_interpreter: bool = False
    agent_enable_image_generation: bool = False
    agent_managed: bool = False

    mistral_mcp_enabled: bool = False
    mistral_mcp_name: str = "messenger_mcp"
    mistral_mcp_server: str = ""
    mistral_mcp_visibility: str = "shared_workspace"
    mistral_mcp_credentials_name: str = "default"


PERSISTED_FIELDS = {
    name for name in Settings.model_fields
    if name != "database_path"
}

SECRET_FIELDS = {
    "telegram_bot_token",
    "telegram_webhook_secret",
    "admin_token",
    "mistral_api_key",
}


@lru_cache
def get_bootstrap_settings() -> Settings:
    return Settings()
