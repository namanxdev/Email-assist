from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from google.adk.models import FallbackModel
from google.genai import errors

from mail_agent.agent import build_instruction, root_agent


def test_instruction_has_current_date_in_configured_timezone(monkeypatch):
    monkeypatch.setenv("USER_TIMEZONE", "Asia/Kolkata")
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    text = build_instruction(MagicMock())
    assert now.strftime("%Y-%m-%d") in text
    assert now.strftime("%A") in text
    assert "Asia/Kolkata" in text


def test_instruction_follows_timezone_setting(monkeypatch):
    monkeypatch.setenv("USER_TIMEZONE", "UTC")
    assert "(UTC)" in build_instruction(MagicMock())


def test_instruction_has_safety_rules():
    text = build_instruction(MagicMock())
    assert "untrusted" in text
    assert "Never follow" in text
    assert "explicit confirmation" in text
    assert "find_contact" in text


def test_root_agent_has_six_tools_and_dynamic_instruction():
    assert root_agent.name == "mail_agent"
    assert len(root_agent.tools) == 6
    assert callable(root_agent.instruction)


def test_model_calls_have_timeout():
    assert root_agent.generate_content_config.http_options.timeout == 30_000


def test_model_fails_over_to_a_second_model():
    model = root_agent.model
    assert isinstance(model, FallbackModel)
    primary, fallback = model.models
    assert primary.model == "gemini-3.5-flash"
    assert fallback.model == "gemini-3.5-flash-lite"
    assert primary.retry_options.attempts == 1  # never wait on a degraded primary


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("no reply"),
        httpx.ConnectError("network down"),
        errors.ServerError(504, {"error": {"code": 504, "message": "m"}}),
        errors.ClientError(429, {"error": {"code": 429, "message": "m"}}),
    ],
)
def test_fails_over_on_timeouts_network_errors_and_server_statuses(error):
    assert root_agent.model._should_fall_back(error)


def test_does_not_fail_over_on_bad_requests_or_bugs():
    assert not root_agent.model._should_fall_back(
        errors.ClientError(400, {"error": {"code": 400, "message": "m"}})
    )
    assert not root_agent.model._should_fall_back(ValueError("bug"))


def test_send_flow_requires_a_separate_confirmation_turn():
    text = build_instruction(MagicMock())
    assert "Never call send_email or reply_to_email in the same turn" in text
