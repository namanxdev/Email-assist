"""Environment-driven configuration. Values are read at call time so tests can monkeypatch."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Microsoft Graph delegated scopes. Do NOT add offline_access/openid/profile: MSAL adds them
# itself and raises if they are passed explicitly.
GRAPH_SCOPES: list[str] = ["User.Read", "Mail.Read", "Mail.Send"]

TOKEN_CACHE_PATH: Path = Path(__file__).resolve().parent.parent / "token_cache.json"

load_dotenv()


@dataclass(frozen=True)
class Settings:
    gemini_model: str
    gemini_timeout_s: int
    ms_client_id: str
    ms_tenant: str
    user_timezone: str
    graph_bearer_token: str


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def get_settings() -> Settings:
    """Read settings from the environment (with defaults) at call time."""
    return Settings(
        gemini_model=_env("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        gemini_timeout_s=int(_env("GEMINI_TIMEOUT_SECONDS", "60")),
        ms_client_id=_env("MS_CLIENT_ID"),
        ms_tenant=_env("MS_TENANT", "common"),
        user_timezone=_env("USER_TIMEZONE", "Asia/Kolkata"),
        graph_bearer_token=_env("GRAPH_BEARER_TOKEN"),
    )
