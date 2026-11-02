from unittest.mock import MagicMock

import msal
import pytest

from mail_agent import auth, config


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TOKEN_CACHE_PATH", tmp_path / "token_cache.json")
    monkeypatch.setenv("MS_CLIENT_ID", "client-123")
    monkeypatch.delenv("GRAPH_BEARER_TOKEN", raising=False)


def _fake_app(monkeypatch, **attrs) -> MagicMock:
    app = MagicMock()
    for key, value in attrs.items():
        setattr(app, key, value)
    monkeypatch.setattr(msal, "PublicClientApplication", MagicMock(return_value=app))
    return app


def test_bearer_token_skips_msal(monkeypatch):
    monkeypatch.setenv("GRAPH_BEARER_TOKEN", "  static-token ")
    boom = MagicMock(side_effect=AssertionError("msal must not be constructed"))
    monkeypatch.setattr(msal, "PublicClientApplication", boom)
    assert auth.get_access_token() == "static-token"
    boom.assert_not_called()


def test_no_accounts_raises_auth_required_with_exact_message(monkeypatch):
    _fake_app(monkeypatch, get_accounts=MagicMock(return_value=[]))
    with pytest.raises(auth.AuthRequired) as exc:
        auth.get_access_token()
    assert str(exc.value) == "Not signed in. Run: uv run python -m mail_agent.auth login"


def test_silent_success_returns_token(monkeypatch):
    app = _fake_app(
        monkeypatch,
        get_accounts=MagicMock(return_value=[{"home_account_id": "a"}]),
        acquire_token_silent=MagicMock(return_value={"access_token": "tok"}),
    )
    assert auth.get_access_token(force_refresh=True) == "tok"
    app.acquire_token_silent.assert_called_once_with(
        ["User.Read", "Mail.Read", "Mail.Send"],
        account={"home_account_id": "a"},
        force_refresh=True,
    )


def test_silent_failure_raises_auth_required(monkeypatch):
    _fake_app(
        monkeypatch,
        get_accounts=MagicMock(return_value=[{"home_account_id": "a"}]),
        acquire_token_silent=MagicMock(return_value=None),
    )
    with pytest.raises(auth.AuthRequired):
        auth.get_access_token()


def test_missing_client_id_raises_auth_required(monkeypatch):
    monkeypatch.delenv("MS_CLIENT_ID")
    with pytest.raises(auth.AuthRequired, match="MS_CLIENT_ID"):
        auth.get_access_token()


def test_msal_constructed_with_authority_and_no_reserved_scopes(monkeypatch):
    monkeypatch.setenv("MS_TENANT", "common")
    ctor = MagicMock(return_value=MagicMock(get_accounts=MagicMock(return_value=[])))
    monkeypatch.setattr(msal, "PublicClientApplication", ctor)
    with pytest.raises(auth.AuthRequired):
        auth.get_access_token()
    assert ctor.call_args.args == ("client-123",)
    assert ctor.call_args.kwargs["authority"] == "https://login.microsoftonline.com/common"
    assert not {"offline_access", "openid", "profile"} & set(config.GRAPH_SCOPES)
