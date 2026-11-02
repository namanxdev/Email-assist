"""All Microsoft Graph HTTP calls: auth header, retries, error mapping."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from . import auth
from .auth import AuthRequired

BASE_URL = "https://graph.microsoft.com/v1.0"
SELECT = "id,subject,from,toRecipients,receivedDateTime,bodyPreview,isRead"
MAX_RETRIES = 3
DEFAULT_RETRY_AFTER = 2.0
MAX_RETRY_AFTER = 30.0
EPOCH = "1900-01-01T00:00:00Z"

TokenProvider = Callable[[bool], str]
Sleep = Callable[[float], Awaitable[None]]


class GraphError(Exception):
    """A non-successful Graph response, with the Graph error code/message when present."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"Graph API error {status_code}: {message}")
        self.status_code = status_code
        self.message = message


def _error_message(resp: httpx.Response) -> str:
    try:
        error = resp.json()["error"]
        return f"{error.get('code', 'Error')}: {error.get('message', resp.reason_phrase)}"
    except (ValueError, KeyError, TypeError, AttributeError):
        return resp.reason_phrase or "Unknown error"


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    """Honour Retry-After when Graph sends it; otherwise back off exponentially (2s, 4s, 8s)."""
    fallback = DEFAULT_RETRY_AFTER * 2 ** (attempt - 1)
    header = resp.headers.get("Retry-After") if resp is not None else None
    try:
        delay = float(header) if header is not None else fallback
    except ValueError:
        delay = fallback
    return min(max(delay, 0.0), MAX_RETRY_AFTER)


def _should_retry(method: str, status: int) -> bool:
    """429/503 mean Graph did not process the request, so they are safe for any method.

    502/504 are ambiguous (a gateway may have forwarded the request), so only idempotent
    GETs are retried on them. Retrying a POST /sendMail there could send the email twice.
    """
    if status in (429, 503):
        return True
    return method == "GET" and status in (502, 504)


def _transport_retryable(method: str, exc: httpx.TransportError) -> bool:
    """A connect failure means nothing was sent; any other transport error is only safe for GET."""
    return method == "GET" or isinstance(exc, httpx.ConnectError)


def _odata_str(value: str) -> str:
    """Escape a value for use inside a single-quoted OData string literal."""
    return value.replace("'", "''")


def _search_expr(text: str) -> str:
    """Wrap text as a KQL $search phrase, escaping inner double quotes."""
    return '"' + text.replace('"', '\\"') + '"'


def _newest_first(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(messages, key=lambda m: m.get("receivedDateTime") or "", reverse=True)


def _default_token_provider(force_refresh: bool) -> str:
    return auth.get_access_token(force_refresh=force_refresh)


class GraphClient:
    def __init__(
        self,
        token_provider: TokenProvider | None = None,
        sleep: Sleep | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._token_provider = token_provider or _default_token_provider
        self._sleep = sleep or asyncio.sleep
        self._http = http or httpx.AsyncClient(base_url=BASE_URL, timeout=30.0)

    async def __aenter__(self) -> "GraphClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _token(self, force_refresh: bool) -> str:
        # MSAL is synchronous and may hit the network on refresh: keep it off the event loop.
        return await asyncio.to_thread(self._token_provider, force_refresh)

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Encode the query ourselves: spaces as %20 (not "+"), which OData parsers expect.
        query = urlencode(params or {}, quote_via=quote, safe="$/,:")
        url = f"{path}?{query}" if query else path
        token = await self._token(False)
        refreshed = False
        retries = 0
        while True:
            headers = {
                "Authorization": f"Bearer {token}",
                "Prefer": 'outlook.body-content-type="text"',
            }
            try:
                resp = await self._http.request(method, url, json=json, headers=headers)
            except httpx.TransportError as exc:
                if retries >= MAX_RETRIES or not _transport_retryable(method, exc):
                    raise
                retries += 1
                await self._sleep(_retry_delay(None, retries))
                continue
            if resp.status_code == 401:
                if refreshed:
                    raise AuthRequired("Microsoft rejected the access token. Sign in again.")
                refreshed = True
                token = await self._token(True)
            elif _should_retry(method, resp.status_code) and retries < MAX_RETRIES:
                retries += 1
                await self._sleep(_retry_delay(resp, retries))
            else:
                break
        if resp.status_code in (202, 204):
            return {}
        if not resp.is_success:
            raise GraphError(resp.status_code, _error_message(resp))
        return resp.json() if resp.content else {}

    async def list_messages(
        self,
        folder: str = "inbox",
        top: int = 10,
        unread_only: bool = False,
        sender: str | None = None,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Newest-first messages in a folder. `since` must be a UTC ISO string (ending in Z)."""
        path = f"/me/mailFolders/{quote(folder, safe='')}/messages"
        if sender and "@" not in sender:
            return await self._list_by_sender_name(path, top, unread_only, sender, since)
        # Graph's InefficientFilter rule: every property used in $orderby must also appear in
        # $filter, first and in the same order. So the receivedDateTime clause always leads.
        clauses = [f"receivedDateTime ge {since or EPOCH}"]
        if unread_only:
            clauses.append("isRead eq false")
        if sender:
            clauses.append(f"from/emailAddress/address eq '{_odata_str(sender)}'")
        params = {
            "$select": SELECT,
            "$top": str(top),
            "$orderby": "receivedDateTime desc",
            "$filter": " and ".join(clauses),
        }
        data = await self._request("GET", path, params=params)
        return data.get("value", [])

    async def _list_by_sender_name(
        self, path: str, top: int, unread_only: bool, name: str, since: str | None
    ) -> list[dict[str, Any]]:
        # A display name cannot be matched with an exact address filter, so use $search.
        # Graph forbids combining $search with $filter/$orderby on messages, so unread/since
        # are applied client-side and results are sorted newest-first here.
        narrowed = unread_only or bool(since)
        params = {
            "$select": SELECT,
            "$top": str(min(top * 3, 50) if narrowed else top),
            "$search": _search_expr(f"from:{name}"),
        }
        data = await self._request("GET", path, params=params)
        messages = data.get("value", [])
        if unread_only:
            messages = [m for m in messages if not m.get("isRead")]
        if since:
            messages = [m for m in messages if (m.get("receivedDateTime") or "") >= since]
        return _newest_first(messages)[:top]

    async def search_messages(self, query: str, top: int = 10) -> list[dict[str, Any]]:
        """Keyword search over the mailbox. $orderby is unsupported with $search: sort here."""
        params = {"$select": SELECT, "$top": str(top), "$search": _search_expr(query)}
        data = await self._request("GET", "/me/messages", params=params)
        return _newest_first(data.get("value", []))

    async def get_message(self, message_id: str) -> dict[str, Any]:
        params = {"$select": f"{SELECT},body"}
        return await self._request("GET", f"/me/messages/{quote(message_id, safe='')}", params)

    async def send_mail(
        self, to: list[str], subject: str, body: str, cc: list[str] | None = None
    ) -> None:
        def recipients(addresses: list[str]) -> list[dict[str, Any]]:
            return [{"emailAddress": {"address": a}} for a in addresses]

        message: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": recipients(to),
        }
        if cc:
            message["ccRecipients"] = recipients(cc)
        await self._request(
            "POST", "/me/sendMail", json={"message": message, "saveToSentItems": True}
        )

    async def reply(self, message_id: str, comment: str) -> None:
        path = f"/me/messages/{quote(message_id, safe='')}/reply"
        await self._request("POST", path, json={"comment": comment})
