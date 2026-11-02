from mail_agent.config import GRAPH_SCOPES, TOKEN_CACHE_PATH, get_settings


def test_defaults(monkeypatch):
    names = (
        "GEMINI_MODEL",
        "GEMINI_TIMEOUT_SECONDS",
        "MS_CLIENT_ID",
        "MS_TENANT",
        "USER_TIMEZONE",
        "GRAPH_BEARER_TOKEN",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    s = get_settings()
    assert s.gemini_model == "gemini-flash-latest"
    assert s.gemini_timeout_s == 60
    assert s.ms_tenant == "common"
    assert s.user_timezone == "Asia/Kolkata"
    assert s.ms_client_id == ""
    assert s.graph_bearer_token == ""


def test_reads_env_at_call_time_and_strips(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", " my-model ")
    monkeypatch.setenv("MS_CLIENT_ID", "abc")
    assert get_settings().gemini_model == "my-model"
    assert get_settings().ms_client_id == "abc"


def test_scopes_exclude_reserved_ones():
    assert GRAPH_SCOPES == ["User.Read", "Mail.Read", "Mail.Send"]
    assert TOKEN_CACHE_PATH.name == "token_cache.json"
