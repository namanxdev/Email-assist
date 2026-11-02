"""Small JSON-safe shapes returned to the agent, and mappers from raw Graph messages."""

from typing import Any, TypedDict


class Contact(TypedDict):
    name: str
    email: str


class EmailSummary(TypedDict):
    id: str
    sender_name: str
    sender_email: str
    subject: str
    date: str
    preview: str
    is_read: bool


class EmailDetail(EmailSummary):
    to: list[Contact]
    body: str
    truncated: bool


def to_contact(recipient: dict[str, Any] | None) -> Contact:
    """Map a Graph recipient ({"emailAddress": {"name", "address"}}) to a Contact."""
    address = ((recipient or {}).get("emailAddress")) or {}
    return {"name": address.get("name") or "", "email": address.get("address") or ""}


def to_summary(msg: dict[str, Any]) -> EmailSummary:
    sender = to_contact(msg.get("from"))
    return {
        "id": msg.get("id") or "",
        "sender_name": sender["name"],
        "sender_email": sender["email"],
        "subject": msg.get("subject") or "(no subject)",
        "date": msg.get("receivedDateTime") or "",
        "preview": msg.get("bodyPreview") or "",
        "is_read": bool(msg.get("isRead")),
    }


def to_detail(msg: dict[str, Any], max_chars: int) -> EmailDetail:
    """Summary plus recipients and the plain-text body, truncated to `max_chars`."""
    body = (msg.get("body") or {}).get("content") or ""
    return {
        **to_summary(msg),
        "to": [to_contact(r) for r in msg.get("toRecipients") or []],
        "body": body[:max_chars],
        "truncated": len(body) > max_chars,
    }
