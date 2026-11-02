"""The real agent run through ADK's Runner with SSE streaming (the `adk web` streaming toggle).

The Gemini models are replaced by scripted stubs; everything else (instruction, tools,
failover subclass, confirmation gate) is the real configuration.
"""

from typing import Any

import httpx
import pytest
from google.adk.agents import RunConfig
from google.adk.agents.run_config import StreamingMode
from google.adk.events import Event
from google.adk.models import BaseLlm, LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

from mail_agent import tools
from mail_agent.agent import _FailoverOnNetworkErrors, root_agent

SSE = RunConfig(streaming_mode=StreamingMode.SSE)
DRAFT = {"to": ["alice@x.com"], "subject": "Meeting", "body": "Confirmed for tomorrow."}


class ScriptedModel(BaseLlm):
    """Each call plays the next script step: responses to yield, or an exception to raise."""

    script: list[list[Any]] = []
    stream_flags: list[bool] = []

    async def generate_content_async(self, llm_request, stream: bool = False):
        self.stream_flags.append(stream)
        for item in self.script.pop(0):
            if isinstance(item, Exception):
                raise item
            yield item


def text(value: str, partial: bool) -> LlmResponse:
    content = types.Content(role="model", parts=[types.Part(text=value)])
    return LlmResponse(content=content, partial=partial)


def call_send_email() -> LlmResponse:
    call = types.FunctionCall(name="send_email", args=DRAFT)
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=call)]))


class Session:
    def __init__(self, model: BaseLlm) -> None:
        agent = root_agent.clone(update={"model": model})
        self.runner = InMemoryRunner(agent=agent, app_name="mail_agent")
        self.session_id = ""

    async def send(self, part: types.Part) -> list[Event]:
        if not self.session_id:
            session = await self.runner.session_service.create_session(
                app_name="mail_agent", user_id="u"
            )
            self.session_id = session.id
        message = types.Content(role="user", parts=[part])
        stream = self.runner.run_async(
            user_id="u", session_id=self.session_id, new_message=message, run_config=SSE
        )
        return [event async for event in stream]


def texts(events: list[Event], partial: bool) -> list[str]:
    return [
        p.text
        for e in events
        if e.content and bool(e.partial) == partial
        for p in e.content.parts
        if p.text
    ]


def confirmation_id(events: list[Event]) -> str:
    calls = [c for e in events for c in e.get_function_calls()]
    return next(c.id for c in calls if c.name == "adk_request_confirmation")


@pytest.fixture
def outbox(monkeypatch) -> list[tuple]:
    sent: list[tuple] = []

    class FakeGraph:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return None

        async def send_mail(self, *args):
            sent.append(args)

    monkeypatch.setattr(tools, "_client", FakeGraph)
    return sent


async def test_streams_partial_text_then_the_final_answer():
    model = ScriptedModel(model="m", script=[[text("Alice ", True), text("Alice sent it.", False)]])
    events = await Session(model).send(types.Part(text="Who sent the latest email?"))
    assert model.stream_flags == [True]
    assert texts(events, partial=True) == ["Alice "]
    assert texts(events, partial=False) == ["Alice sent it."]


async def test_streaming_fails_over_when_primary_times_out_before_output():
    primary = ScriptedModel(model="primary", script=[[httpx.ReadTimeout("stalled")]])
    backup = ScriptedModel(model="backup", script=[[text("Hi", True), text("Hi there", False)]])
    model = _FailoverOnNetworkErrors(models=[primary, backup])
    events = await Session(model).send(types.Part(text="hello"))
    assert texts(events, partial=False) == ["Hi there"]
    assert backup.stream_flags == [True]


async def test_streaming_never_switches_models_after_output_started():
    primary = ScriptedModel(model="primary", script=[[text("Hal", True), httpx.ReadTimeout("x")]])
    backup = ScriptedModel(model="backup", script=[[text("never", False)]])
    model = _FailoverOnNetworkErrors(models=[primary, backup])
    with pytest.raises(httpx.ReadTimeout):
        await Session(model).send(types.Part(text="hello"))
    assert backup.stream_flags == []  # a turn is never stitched together from two models


@pytest.mark.parametrize(("approved", "expected_sends"), [(False, 0), (True, 1)])
async def test_streaming_send_waits_for_approval(outbox, approved, expected_sends):
    model = ScriptedModel(model="m", script=[[text("Sending", True), call_send_email()]])
    session = Session(model)
    events = await session.send(types.Part(text="yes"))
    assert outbox == []  # paused for approval, nothing sent yet

    model.script.append([text("Done.", False)])
    approval = types.FunctionResponse(
        id=confirmation_id(events),
        name="adk_request_confirmation",
        response={"confirmed": approved},
    )
    await session.send(types.Part(function_response=approval))
    assert len(outbox) == expected_sends
    if approved:
        assert outbox[0][:3] == (DRAFT["to"], DRAFT["subject"], DRAFT["body"])
