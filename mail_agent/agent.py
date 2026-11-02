from google.adk.agents import Agent

from .config import get_settings

root_agent = Agent(
    name="mail_agent",
    model=get_settings().gemini_model,
    description="Reads and sends Outlook email through Microsoft Graph.",
    instruction="You are an email assistant.",
)
