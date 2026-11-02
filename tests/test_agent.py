from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

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
