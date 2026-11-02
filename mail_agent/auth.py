"""MSAL browser login (auth code + PKCE), token cache, and `get_access_token()` for Graph."""

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

import msal

from . import config

NOT_SIGNED_IN = "Not signed in. Run: uv run mail-login"
REDIRECT_URI_HINT = (
    'Add the redirect URI http://localhost under the "Mobile and desktop applications" '
    "platform in Entra (App registrations > your app > Authentication)."
)


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


def _login_error(result: dict[str, Any]) -> str:
    """User-facing message for a failed interactive sign-in, with a fix for AADSTS50011."""
    description = result.get("error_description") or result.get("error") or "unknown error"
    message = f"Sign-in failed: {description}"
    if "AADSTS50011" in description:
        message += f"\n{REDIRECT_URI_HINT}"
    return message


def login() -> None:
    """Interactive browser sign-in (auth code + PKCE). MSAL opens the system browser.

    MSAL listens on http://localhost with a random port for the redirect.
    """
    cache = _load_cache()
    app = _build_app(cache)
    print("Opening your browser to sign in...")
    result = app.acquire_token_interactive(scopes=config.GRAPH_SCOPES, prompt="select_account")
    if "access_token" not in result:
        raise RuntimeError(_login_error(result))
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


def signed_in_addresses() -> set[str]:
    """Lowercased usernames of the accounts in the token cache. Never raises.

    Read straight from the cache file (no client id, no network). Returns an empty set when
    no address is known: GRAPH_BEARER_TOKEN is set, there is no cache, or it is unreadable.
    """
    if config.get_settings().graph_bearer_token:
        return set()
    try:
        accounts = list(_load_cache().search(msal.TokenCache.CredentialType.ACCOUNT))
    except (OSError, ValueError):
        return set()
    return {a["username"].strip().lower() for a in accounts if a.get("username")}


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
