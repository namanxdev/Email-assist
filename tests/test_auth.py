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
    assert str(exc.value) == "Not signed in. Run: uv run mail-login"


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


def test_login_opens_browser_saves_cache_and_never_prints_token(monkeypatch, capsys):
    cache = MagicMock(has_state_changed=True)
    cache.serialize.return_value = '{"cached": true}'
    monkeypatch.setattr(auth, "_load_cache", MagicMock(return_value=cache))
    app = _fake_app(
        monkeypatch,
        acquire_token_interactive=MagicMock(
            return_value={
                "access_token": "secret-token",
                "id_token_claims": {"preferred_username": "me@x.com"},
            }
        ),
    )
    auth.login()
    out = capsys.readouterr().out
    assert "Opening your browser to sign in..." in out
    assert "Signed in as me@x.com" in out
    assert "secret-token" not in out
    app.acquire_token_interactive.assert_called_once_with(
        scopes=["User.Read", "Mail.Read", "Mail.Send"], prompt="select_account"
    )
    assert config.TOKEN_CACHE_PATH.read_text(encoding="utf-8") == '{"cached": true}'


def test_login_failure_reports_error_description(monkeypatch):
    _fake_app(
        monkeypatch,
        acquire_token_interactive=MagicMock(
            return_value={"error": "access_denied", "error_description": "User declined consent"}
        ),
    )
    with pytest.raises(RuntimeError, match="Sign-in failed: User declined consent") as exc:
        auth.login()
    assert "http://localhost" not in str(exc.value)
    assert not config.TOKEN_CACHE_PATH.exists()


def test_login_redirect_uri_mismatch_explains_entra_fix(monkeypatch):
    description = "AADSTS50011: The redirect URI 'http://localhost:51234' does not match."
    _fake_app(
        monkeypatch,
        acquire_token_interactive=MagicMock(
            return_value={"error": "invalid_request", "error_description": description}
        ),
    )
    with pytest.raises(RuntimeError) as exc:
        auth.login()
    message = str(exc.value)
    assert description in message
    assert "http://localhost" in message
    assert "Mobile and desktop applications" in message


def test_login_cli_prints_failure_without_traceback(monkeypatch):
    _fake_app(
        monkeypatch,
        acquire_token_interactive=MagicMock(return_value={"error_description": "AADSTS50011: x"}),
    )
    with pytest.raises(SystemExit) as exc:
        auth.login_cli()
    assert "AADSTS50011" in str(exc.value)
    assert "Mobile and desktop applications" in str(exc.value)


def test_logout_removes_accounts_and_cache_file(monkeypatch, capsys):
    config.TOKEN_CACHE_PATH.write_text("{}", encoding="utf-8")
    app = _fake_app(monkeypatch, get_accounts=MagicMock(return_value=["acct1", "acct2"]))
    auth.logout()
    assert [c.args[0] for c in app.remove_account.call_args_list] == ["acct1", "acct2"]
    assert not config.TOKEN_CACHE_PATH.exists()
    assert "Signed out" in capsys.readouterr().out


def test_cache_is_written_only_when_state_changed(monkeypatch):
    cache = MagicMock(has_state_changed=False)
    auth._save_cache(cache)
    assert not config.TOKEN_CACHE_PATH.exists()
    cache = MagicMock(has_state_changed=True)
    cache.serialize.return_value = '{"k": 1}'
    auth._save_cache(cache)
    assert config.TOKEN_CACHE_PATH.read_text(encoding="utf-8") == '{"k": 1}'


def test_main_reports_errors_without_traceback(monkeypatch):
    monkeypatch.delenv("MS_CLIENT_ID")
    monkeypatch.setattr("sys.argv", ["auth", "login"])
    with pytest.raises(SystemExit) as exc:
        auth.main()
    assert "MS_CLIENT_ID" in str(exc.value)
