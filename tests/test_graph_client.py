import httpx
import pytest
import respx

from mail_agent.auth import AuthRequired
from mail_agent.graph_client import BASE_URL, SELECT, GraphClient, GraphError

INBOX = f"{BASE_URL}/me/mailFolders/inbox/messages"


class Tokens:
    """Token provider stub that records the force_refresh flag of each call."""

    def __init__(self) -> None:
        self.calls: list[bool] = []

    def __call__(self, force_refresh: bool = False) -> str:
        self.calls.append(force_refresh)
        return "tok-refreshed" if force_refresh else "tok-1"


class Sleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@pytest.fixture
def tokens() -> Tokens:
    return Tokens()


@pytest.fixture
def sleeper() -> Sleeper:
    return Sleeper()


@pytest.fixture
async def client(tokens, sleeper):
    async with GraphClient(token_provider=tokens, sleep=sleeper) as c:
        yield c


def msg(mid: str, received: str, **extra) -> dict:
    return {"id": mid, "receivedDateTime": received, "subject": mid, **extra}


@respx.mock
async def test_list_messages_url_params_and_headers(client):
    route = respx.get(INBOX).respond(200, json={"value": [msg("a", "2026-11-01T10:00:00Z")]})
    result = await client.list_messages(
        top=5, unread_only=True, sender="bob@x.com", since="2026-10-01T00:00:00Z"
    )
    assert [m["id"] for m in result] == ["a"]
    req = route.calls.last.request
    params = req.url.params
    assert params["$select"] == SELECT
    assert params["$top"] == "5"
    assert params["$orderby"] == "receivedDateTime desc"
    assert params["$filter"] == (
        "receivedDateTime ge 2026-10-01T00:00:00Z"
        " and isRead eq false"
        " and from/emailAddress/address eq 'bob@x.com'"
    )
    assert "$search" not in params
    assert req.headers["Authorization"] == "Bearer tok-1"
    assert req.headers["Prefer"] == 'outlook.body-content-type="text"'
    assert "+" not in req.url.query.decode()


@respx.mock
async def test_list_messages_filter_always_starts_with_orderby_property(client):
    route = respx.get(INBOX).respond(200, json={"value": []})
    await client.list_messages()
    assert (
        route.calls.last.request.url.params["$filter"] == "receivedDateTime ge 1900-01-01T00:00:00Z"
    )


@respx.mock
async def test_list_messages_escapes_single_quotes_in_sender(client):
    route = respx.get(INBOX).respond(200, json={"value": []})
    await client.list_messages(sender="o'brien@x.com")
    assert "address eq 'o''brien@x.com'" in route.calls.last.request.url.params["$filter"]


@respx.mock
async def test_list_messages_custom_folder_is_path_encoded(client):
    route = respx.get(f"{BASE_URL}/me/mailFolders/sentitems/messages").respond(
        200, json={"value": []}
    )
    await client.list_messages(folder="sentitems")
    assert route.called


@respx.mock
async def test_list_messages_sender_display_name_uses_search_and_client_side_filters(client):
    route = respx.get(INBOX).respond(
        200,
        json={
            "value": [
                msg("old", "2026-10-01T00:00:00Z", isRead=False),
                msg("read", "2026-11-02T00:00:00Z", isRead=True),
                msg("new", "2026-11-01T00:00:00Z", isRead=False),
                msg("mid", "2026-10-20T00:00:00Z", isRead=False),
            ]
        },
    )
    result = await client.list_messages(
        top=2, unread_only=True, sender="Alice", since="2026-10-15T00:00:00Z"
    )
    params = route.calls.last.request.url.params
    assert params["$search"] == '"from:Alice"'
    assert "$filter" not in params
    assert "$orderby" not in params
    assert params["$top"] == "6"
    assert [m["id"] for m in result] == ["new", "mid"]


@respx.mock
async def test_search_messages_params_and_client_side_sort(client):
    route = respx.get(f"{BASE_URL}/me/messages").respond(
        200,
        json={
            "value": [
                msg("old", "2026-10-01T00:00:00Z"),
                msg("new", "2026-11-01T00:00:00Z"),
                msg("mid", "2026-10-15T00:00:00Z"),
            ]
        },
    )
    result = await client.search_messages('say "hi"', top=3)
    params = route.calls.last.request.url.params
    assert params["$search"] == '"say \\"hi\\""'
    assert params["$top"] == "3"
    assert params["$select"] == SELECT
    assert "$orderby" not in params
    assert "$filter" not in params
    assert [m["id"] for m in result] == ["new", "mid", "old"]


@respx.mock
async def test_get_message_selects_body_and_encodes_id(client):
    route = respx.get(url__startswith=f"{BASE_URL}/me/messages/").respond(
        200, json={"id": "a/b+c=", "body": {"content": "hi"}}
    )
    result = await client.get_message("a/b+c=")
    req = route.calls.last.request
    assert req.url.raw_path.startswith(b"/v1.0/me/messages/a%2Fb%2Bc%3D?")
    assert req.url.params["$select"] == f"{SELECT},body"
    assert result["body"]["content"] == "hi"


@respx.mock
async def test_send_mail_payload(client):
    route = respx.post(f"{BASE_URL}/me/sendMail").respond(202)
    await client.send_mail(["a@x.com", "b@x.com"], "Subj", "Body", cc=["c@x.com"])
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer tok-1"
    assert httpx.Response(200, content=req.content).json() == {
        "message": {
            "subject": "Subj",
            "body": {"contentType": "Text", "content": "Body"},
            "toRecipients": [
                {"emailAddress": {"address": "a@x.com"}},
                {"emailAddress": {"address": "b@x.com"}},
            ],
            "ccRecipients": [{"emailAddress": {"address": "c@x.com"}}],
        },
        "saveToSentItems": True,
    }


@respx.mock
async def test_send_mail_without_cc_omits_cc_recipients(client):
    route = respx.post(f"{BASE_URL}/me/sendMail").respond(202)
    await client.send_mail(["a@x.com"], "S", "B")
    payload = httpx.Response(200, content=route.calls.last.request.content).json()
    assert "ccRecipients" not in payload["message"]


@respx.mock
async def test_reply_posts_comment_to_encoded_path(client):
    route = respx.post(url__startswith=f"{BASE_URL}/me/messages/").respond(202)
    await client.reply("id/1=", "Thanks!")
    req = route.calls.last.request
    assert req.url.raw_path == b"/v1.0/me/messages/id%2F1%3D/reply"
    assert httpx.Response(200, content=req.content).json() == {"comment": "Thanks!"}


@respx.mock
async def test_401_refreshes_token_once_and_retries(client, tokens):
    route = respx.get(INBOX).mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"value": []})]
    )
    assert await client.list_messages() == []
    assert tokens.calls == [False, True]
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok-1"
    assert route.calls[1].request.headers["Authorization"] == "Bearer tok-refreshed"


@respx.mock
async def test_401_twice_raises_auth_required(client, tokens):
    respx.get(INBOX).mock(return_value=httpx.Response(401))
    with pytest.raises(AuthRequired):
        await client.list_messages()
    assert tokens.calls == [False, True]


@respx.mock
async def test_429_honours_retry_after_then_succeeds(client, sleeper):
    route = respx.get(INBOX).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(200, json={"value": []}),
        ]
    )
    assert await client.list_messages() == []
    assert sleeper.delays == [3.0]
    assert route.call_count == 2


@respx.mock
async def test_retry_after_is_capped_and_defaulted(client, sleeper):
    respx.get(INBOX).mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "999"}),
            httpx.Response(429),
            httpx.Response(200, json={"value": []}),
        ]
    )
    await client.list_messages()
    assert sleeper.delays == [30.0, 2.0]


@respx.mock
async def test_gives_up_after_max_retries(client, sleeper):
    route = respx.get(INBOX).mock(
        return_value=httpx.Response(
            429, json={"error": {"code": "TooManyRequests", "message": "slow down"}}
        )
    )
    with pytest.raises(GraphError) as exc:
        await client.list_messages()
    assert exc.value.status_code == 429
    assert route.call_count == 4
    assert len(sleeper.delays) == 3


@respx.mock
async def test_500_maps_to_graph_error_with_message(client):
    respx.get(INBOX).respond(
        500, json={"error": {"code": "ErrorInternalServerError", "message": "Boom"}}
    )
    with pytest.raises(GraphError) as exc:
        await client.list_messages()
    assert exc.value.status_code == 500
    assert "ErrorInternalServerError" in exc.value.message
    assert "Boom" in exc.value.message


@respx.mock
async def test_non_json_error_body_still_maps_to_graph_error(client):
    respx.get(INBOX).respond(502, text="<html>bad gateway</html>")
    with pytest.raises(GraphError) as exc:
        await client.list_messages()
    assert exc.value.status_code == 502
