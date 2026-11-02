"""MSAL device-code login, token cache, and `get_access_token()` for the Graph client."""

import argparse
from collections.abc import Callable
from pathlib import Path

import msal

from . import config

NOT_SIGNED_IN = "Not signed in. Run: uv run mail-login"


class AuthRequired(Exception):
    """Raised when no usable Microsoft access token is available."""

    def __init__(self, message: str = NOT_SIGNED_IN) -> None:
        super().__init__(message)
        self.message = message


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    path: Path = config.TOKEN_CACHE_PATH
    if path.exists():
        cache.deserialize(path.read_text(encoding="utf-8"))
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        config.TOKEN_CACHE_PATH.write_text(cache.serialize(), encoding="utf-8")


def _build_app(cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    settings = config.get_settings()
    if not settings.ms_client_id:
        raise AuthRequired("MS_CLIENT_ID is not set. Copy .env.example to .env and fill it in.")
    return msal.PublicClientApplication(
        settings.ms_client_id,
        authority=f"https://login.microsoftonline.com/{settings.ms_tenant}",
        token_cache=cache,
    )


def login() -> None:
    """Interactive device-code sign-in. Prints the URL and code, then waits for the user."""
    cache = _load_cache()
    app = _build_app(cache)
    flow = app.initiate_device_flow(scopes=config.GRAPH_SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Could not start device flow: {flow.get('error_description', flow)}")
    print("\n" + flow["message"] + "\n")
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(f"Sign-in failed: {result.get('error_description', 'unknown error')}")
    _save_cache(cache)
    username = (result.get("id_token_claims") or {}).get("preferred_username", "unknown user")
    print(f"Signed in as {username}")


def logout() -> None:
    """Remove all cached accounts and delete the token cache file."""
    cache = _load_cache()
    app = _build_app(cache)
    for account in app.get_accounts():
        app.remove_account(account)
    config.TOKEN_CACHE_PATH.unlink(missing_ok=True)
    print("Signed out and token cache removed.")


def get_access_token(force_refresh: bool = False) -> str:
    """Return a valid Graph access token, refreshing silently from the cache when needed.

    GRAPH_BEARER_TOKEN, when set, short-circuits MSAL entirely.
    Raises AuthRequired if the user has not signed in.
    """
    bearer = config.get_settings().graph_bearer_token
    if bearer:
        return bearer
    cache = _load_cache()
    app = _build_app(cache)
    accounts = app.get_accounts()
    if not accounts:
        raise AuthRequired()
    result = app.acquire_token_silent(
        config.GRAPH_SCOPES, account=accounts[0], force_refresh=force_refresh
    )
    _save_cache(cache)
    if not result or "access_token" not in result:
        raise AuthRequired()
    return result["access_token"]


def _run(command: Callable[[], None]) -> None:
    try:
        command()
    except (AuthRequired, RuntimeError) as exc:
        raise SystemExit(f"Error: {exc}") from exc


def login_cli() -> None:
    """Entry point for `uv run mail-login`."""
    _run(login)


def logout_cli() -> None:
    """Entry point for `uv run mail-logout`."""
    _run(logout)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m mail_agent.auth")
    parser.add_argument("command", choices=["login", "logout"])
    args = parser.parse_args()
    _run({"login": login, "logout": logout}[args.command])


if __name__ == "__main__":
    main()
