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


def test_login_prints_message_saves_cache_and_never_prints_token(monkeypatch, capsys, tmp_path):
    app = _fake_app(
        monkeypatch,
        initiate_device_flow=MagicMock(return_value={"user_code": "ABC", "message": "Go to URL"}),
        acquire_token_by_device_flow=MagicMock(
            return_value={
                "access_token": "secret-token",
                "id_token_claims": {"preferred_username": "me@x.com"},
            }
        ),
    )
    auth.login()
    out = capsys.readouterr().out
    assert "Go to URL" in out
    assert "Signed in as me@x.com" in out
    assert "secret-token" not in out
    app.initiate_device_flow.assert_called_once_with(scopes=["User.Read", "Mail.Read", "Mail.Send"])


def test_login_fails_when_device_flow_cannot_start(monkeypatch):
    _fake_app(
        monkeypatch,
        initiate_device_flow=MagicMock(return_value={"error_description": "bad client id"}),
    )
    with pytest.raises(RuntimeError, match="bad client id"):
        auth.login()


def test_login_fails_when_token_not_granted(monkeypatch):
    _fake_app(
        monkeypatch,
        initiate_device_flow=MagicMock(return_value={"user_code": "A", "message": "m"}),
        acquire_token_by_device_flow=MagicMock(return_value={"error_description": "declined"}),
    )
    with pytest.raises(RuntimeError, match="declined"):
        auth.login()


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
