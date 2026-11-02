from unittest.mock import MagicMock

import httpx
import pytest
from google.adk.tools.tool_confirmation import ToolConfirmation

from mail_agent import tools
from mail_agent.auth import AuthRequired
from mail_agent.graph_client import GraphError


class FakeGraph:
    """Stands in for GraphClient: async context manager that records calls."""

    def __init__(self, messages=None, message=None, error: Exception | None = None) -> None:
        self.messages = messages or []
        self.message = message or {}
        self.error = error
        self.calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    def _record(self, name: str, *args):
        self.calls.append((name, *args))
        if self.error:
            raise self.error

    async def list_messages(self, folder, top, unread_only, sender, since):
        self._record("list_messages", folder, top, unread_only, sender, since)
        return self.messages

    async def search_messages(self, query, top=10):
        self._record("search_messages", query, top)
        return self.messages

    async def get_message(self, message_id):
        self._record("get_message", message_id)
        return self.message

    async def send_mail(self, to, subject, body, cc=None):
        self._record("send_mail", to, subject, body, cc)

    async def reply(self, message_id, comment):
        self._record("reply", message_id, comment)


@pytest.fixture(autouse=True)
def tz(monkeypatch):
    monkeypatch.setenv("USER_TIMEZONE", "Asia/Kolkata")


@pytest.fixture
def use_graph(monkeypatch):
    def install(graph: FakeGraph) -> FakeGraph:
        monkeypatch.setattr(tools, "_client", lambda: graph)
        return graph

    return install


def person(name: str, address: str) -> dict:
    return {"emailAddress": {"name": name, "address": address}}


def msg(mid: str, received: str, **extra) -> dict:
    return {
        "id": mid,
        "subject": f"subject {mid}",
        "receivedDateTime": received,
        "from": person("Alice", "alice@x.com"),
        "bodyPreview": "preview",
        "isRead": False,
        **extra,
    }


def ids(result: dict) -> list[str]:
    return [e["id"] for e in result["emails"]]


# list_emails


async def test_list_emails_maps_summaries_and_clamps_top(use_graph):
    graph = use_graph(FakeGraph(messages=[msg("a", "2026-11-01T10:00:00Z")]))
    result = await tools.list_emails(top=500, unread_only=True, sender="Alice")
    assert graph.calls == [("list_messages", "inbox", 50, True, "Alice", None)]
    assert result["status"] == "success"
    assert result["count"] == 1
    assert result["emails"][0] == {
        "id": "a",
        "sender_name": "Alice",
        "sender_email": "alice@x.com",
        "subject": "subject a",
        "date": "2026-11-01T10:00:00Z",
        "preview": "preview",
        "is_read": False,
    }


async def test_list_emails_clamps_low_top(use_graph):
    graph = use_graph(FakeGraph())
    await tools.list_emails(top=0)
    assert graph.calls[0][2] == 1


async def test_list_emails_bare_date_is_user_timezone_converted_to_utc(use_graph):
    graph = use_graph(FakeGraph())
    await tools.list_emails(since="2026-11-01")
    assert graph.calls[0][-1] == "2026-10-31T18:30:00Z"


async def test_list_emails_since_datetime_with_offset_normalized(use_graph):
    graph = use_graph(FakeGraph())
    await tools.list_emails(since="2026-11-01T10:00:00+05:30")
    assert graph.calls[0][-1] == "2026-11-01T04:30:00Z"


async def test_list_emails_invalid_since_returns_error(use_graph):
    graph = use_graph(FakeGraph())
    result = await tools.list_emails(since="yesterday")
    assert result["status"] == "error"
    assert "Invalid date" in result["message"]
    assert graph.calls == []


# search_emails


async def test_search_emails_applies_since_until_inclusive_of_whole_day(use_graph):
    # Asia/Kolkata day 2026-11-01 == 2026-10-31T18:30Z .. 2026-11-01T18:30Z (exclusive)
    graph = use_graph(
        FakeGraph(
            messages=[
                msg("before", "2026-10-31T18:29:59Z"),
                msg("start", "2026-10-31T18:30:00Z"),
                msg("end", "2026-11-01T18:29:59Z"),
                msg("after", "2026-11-01T18:30:00Z"),
            ]
        )
    )
    result = await tools.search_emails("meeting", since="2026-11-01", until="2026-11-01")
    assert ids(result) == ["start", "end"]
    assert graph.calls == [("search_messages", "meeting", 30)]


async def test_search_emails_keeps_newest_first_order_and_cuts_to_top(use_graph):
    # The Graph client already returns newest-first; the tool keeps that order and cuts to top.
    graph = use_graph(
        FakeGraph(messages=[msg("new", "2026-11-02T00:00:00Z"), msg("old", "2026-10-01T00:00:00Z")])
    )
    result = await tools.search_emails("x", top=1)
    assert ids(result) == ["new"]
    assert graph.calls == [("search_messages", "x", 1)]


async def test_search_emails_rejects_empty_query(use_graph):
    graph = use_graph(FakeGraph())
    result = await tools.search_emails("   ")
    assert result["status"] == "error"
    assert graph.calls == []


# get_email


async def test_get_email_truncates_long_body(use_graph):
    long_body = "a" * 5000
    use_graph(
        FakeGraph(
            message=msg(
                "m1",
                "2026-11-01T00:00:00Z",
                body={"content": long_body},
                toRecipients=[person("Me", "me@x.com")],
            )
        )
    )
    result = await tools.get_email("m1")
    email = result["email"]
    assert result["status"] == "success"
    assert len(email["body"]) == 4000
    assert email["truncated"] is True
    assert email["to"] == [{"name": "Me", "email": "me@x.com"}]


async def test_get_email_keeps_short_body_intact(use_graph):
    use_graph(FakeGraph(message=msg("m1", "2026-11-01T00:00:00Z", body={"content": "hello"})))
    email = (await tools.get_email("m1"))["email"]
    assert email["body"] == "hello"
    assert email["truncated"] is False


# find_contact


async def test_find_contact_dedupes_case_insensitively(use_graph):
    graph = use_graph(
        FakeGraph(
            messages=[
                msg("1", "2026-11-01T00:00:00Z", **{"from": person("Priya S", "Priya@Acme.com")}),
                msg("2", "2026-11-01T00:00:00Z", **{"from": person("Priya S", "priya@acme.com")}),
                msg(
                    "3",
                    "2026-11-01T00:00:00Z",
                    **{"from": person("Bob", "bob@x.com")},
                    toRecipients=[
                        person("Priya Sharma", "priya.sharma@other.com"),
                        person("Zed", "zed@x.com"),
                    ],
                ),
            ]
        )
    )
    result = await tools.find_contact("priya")
    assert graph.calls == [("search_messages", "participants:priya", 25)]
    assert result["status"] == "success"
    assert result["count"] == 2
    assert [m["email"].lower() for m in result["matches"]] == [
        "priya@acme.com",
        "priya.sharma@other.com",
    ]


async def test_find_contact_no_match(use_graph):
    use_graph(FakeGraph(messages=[msg("1", "2026-11-01T00:00:00Z")]))
    result = await tools.find_contact("nobody")
    assert result == {"status": "success", "count": 0, "matches": []}


# error handling


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AuthRequired(), "Not signed in"),
        (GraphError(500, "Boom"), "Boom"),
        (httpx.ConnectError("down"), "Network error"),
    ],
)
async def test_errors_become_error_dicts(use_graph, error, expected):
    use_graph(FakeGraph(error=error))
    for call in (
        tools.list_emails(),
        tools.search_emails("x"),
        tools.get_email("id"),
        tools.find_contact("x"),
        tools.send_email(["a@x.com"], "s", "b"),
        tools.reply_to_email("id", "hi"),
    ):
        result = await call
        assert result["status"] == "error"
        assert expected in result["message"]


# send / reply


async def test_send_email_success_and_validation(use_graph):
    graph = use_graph(FakeGraph())
    ok = await tools.send_email([" a@x.com "], "Hi", "Body", cc=["c@x.com"])
    assert ok == {"status": "success", "sent_to": ["a@x.com"], "cc": ["c@x.com"], "subject": "Hi"}
    assert graph.calls == [("send_mail", ["a@x.com"], "Hi", "Body", ["c@x.com"])]

    graph.calls.clear()
    bad = await tools.send_email(["not-an-address"], "Hi", "Body")
    assert bad["status"] == "error"
    assert "not-an-address" in bad["message"]
    none = await tools.send_email([], "Hi", "Body")
    assert none["status"] == "error"
    assert graph.calls == []


async def test_reply_to_email(use_graph):
    graph = use_graph(FakeGraph())
    assert await tools.reply_to_email("id1", "Thanks") == {"status": "success", "email_id": "id1"}
    assert graph.calls == [("reply", "id1", "Thanks")]
    assert (await tools.reply_to_email("id1", " "))["status"] == "error"


# confirmation gate


def tool_context(confirmation: ToolConfirmation | None) -> MagicMock:
    ctx = MagicMock()
    ctx.tool_confirmation = confirmation
    return ctx


SEND_ARGS = {"to": ["a@x.com"], "subject": "Hi", "body": "Body"}


async def test_send_email_cannot_run_without_confirmation(use_graph):
    graph = use_graph(FakeGraph())
    ctx = tool_context(None)
    result = await tools.send_email_tool.run_async(args=SEND_ARGS, tool_context=ctx)
    assert "requires confirmation" in result["error"]
    ctx.request_confirmation.assert_called_once()
    assert graph.calls == []


async def test_send_email_rejected_does_not_send(use_graph):
    graph = use_graph(FakeGraph())
    ctx = tool_context(ToolConfirmation(confirmed=False))
    result = await tools.send_email_tool.run_async(args=SEND_ARGS, tool_context=ctx)
    assert result == {"error": "This tool call is rejected."}
    assert graph.calls == []


async def test_send_email_confirmed_sends(use_graph):
    graph = use_graph(FakeGraph())
    ctx = tool_context(ToolConfirmation(confirmed=True))
    result = await tools.send_email_tool.run_async(args=SEND_ARGS, tool_context=ctx)
    assert result["status"] == "success"
    assert [c[0] for c in graph.calls] == ["send_mail"]


async def test_reply_requires_confirmation_too(use_graph):
    graph = use_graph(FakeGraph())
    args = {"email_id": "id1", "body": "ok"}
    blocked = await tools.reply_to_email_tool.run_async(args=args, tool_context=tool_context(None))
    assert "requires confirmation" in blocked["error"]
    assert graph.calls == []
    ctx = tool_context(ToolConfirmation(confirmed=True))
    assert (await tools.reply_to_email_tool.run_async(args=args, tool_context=ctx))[
        "status"
    ] == "success"


def test_all_tools_registered():
    names = [getattr(t, "name", getattr(t, "__name__", None)) for t in tools.ALL_TOOLS]
    assert names == [
        "list_emails",
        "search_emails",
        "get_email",
        "find_contact",
        "send_email",
        "reply_to_email",
    ]
