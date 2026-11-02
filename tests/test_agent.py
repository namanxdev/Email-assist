from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from google.adk.models import FallbackModel

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


def test_model_fails_over_to_a_second_model_on_server_errors():
    model = root_agent.model
    assert isinstance(model, FallbackModel)
    primary, fallback = model.models
    assert primary.model == "gemini-3.5-flash"
    assert fallback.model == "gemini-3.5-flash-lite"
    # Network blips are retried on the primary; error statuses go straight to the fallback.
    assert primary.retry_options.attempts == 2
    assert primary.retry_options.http_status_codes == [408]
    assert {429, 503, 504} <= set(model.retriable_status_codes)


def test_send_flow_requires_a_separate_confirmation_turn():
    text = build_instruction(MagicMock())
    assert "Never call send_email or reply_to_email in the same turn" in text
