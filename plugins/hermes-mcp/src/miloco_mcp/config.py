"""Miloco MCP Server — Configuration."""

import json
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_token_from_miloco_config() -> str:
    """Auto-load server.token from Miloco's config.json."""
    mioco_home = os.environ.get("MILOCO_HOME", "")
    if mioco_home:
        cfg_path = Path(mioco_home) / "config.json"
    else:
        cfg_path = Path.home() / ".openclaw" / "miloco" / "config.json"
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        return data.get("server", {}).get("token", "")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="MILOCO_",
        extra="ignore",
    )

    base_url: str = "http://127.0.0.1:1810"
    token: str = ""
    timeout: float = 30.0
    tls_verify: bool = False

    def get_token(self) -> str:
        """Return configured token, or auto-load from Miloco config."""
        if self.token:
            return self.token
        return _load_token_from_miloco_config()


settings = Settings()
