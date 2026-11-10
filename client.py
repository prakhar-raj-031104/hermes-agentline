"""Thin AgentLine REST client (stdlib only).

Every request goes to ``https://api.agentline.cloud`` with the account's API key as a Bearer
token. The key is read through Hermes's secret scope, never from a file.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

API_BASE = "https://api.agentline.cloud"
WS_BASE = "wss://api.agentline.cloud"
API_KEY_ENV = "AGENTLINE_API_KEY"
AGENT_ID_ENV = "AGENTLINE_AGENT_ID"
USER_AGENT = "hermes-agentline/1.0"

TERMINAL_CALL_STATUSES = frozenset({"completed", "failed", "busy", "no-answer", "canceled"})


class AgentLineError(Exception):
    """An AgentLine request failed. ``status`` is the HTTP status, or 0 for a transport error."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"AgentLine HTTP {status}: {detail}" if status else f"AgentLine request failed: {detail}")
        self.status = status
        self.detail = detail


def _error_detail(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        return text[:500]
    detail = parsed.get("detail", parsed) if isinstance(parsed, dict) else parsed
    return (detail if isinstance(detail, str) else json.dumps(detail))[:500]


class AgentLineClient:
    """Synchronous client. Async callers wrap calls in ``asyncio.to_thread``."""

    def __init__(self, api_key: str, timeout: float = 30.0):
        self._api_key = api_key
        self._timeout = timeout

    # -- transport -----------------------------------------------------------

    def _send(self, request: urllib.request.Request) -> bytes:
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return response.read()

    def request(self, method: str, path: str, *, params: Optional[dict] = None,
                body: Optional[dict] = None, headers: Optional[dict] = None) -> Any:
        query = {k: v for k, v in (params or {}).items() if v is not None}
        url = API_BASE + path + ("?" + urllib.parse.urlencode(query) if query else "")
        all_headers = {"Accept": "application/json", "User-Agent": USER_AGENT,
                       "Authorization": f"Bearer {self._api_key}", **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps({k: v for k, v in body.items() if v is not None}).encode("utf-8")
            all_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=all_headers, method=method)
        try:
            raw = self._send(request)
        except urllib.error.HTTPError as exc:
            raise AgentLineError(exc.code, _error_detail(exc.read())) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AgentLineError(0, str(getattr(exc, "reason", exc))) from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise AgentLineError(0, "response was not JSON") from None

    # -- account -------------------------------------------------------------

    def balance(self) -> dict:
        return self.request("GET", "/v1/billing/balance")

    def list_agents(self) -> list:
        return self.request("GET", "/v1/agents")

    def get_agent(self, agent_id: str) -> dict:
        return self.request("GET", f"/v1/agents/{agent_id}")

    def create_agent(self, fields: dict) -> dict:
        return self.request("POST", "/v1/agents", body=fields)

    def update_agent(self, agent_id: str, fields: dict) -> dict:
        return self.request("PATCH", f"/v1/agents/{agent_id}", body=fields)

    def list_numbers(self) -> list:
        return self.request("GET", "/v1/numbers")

    def buy_number(self, agent_id: str, country: str = "US", area_code: Optional[str] = None) -> dict:
        return self.request("POST", "/v1/numbers",
                            body={"agent_id": agent_id, "country": country, "area_code": area_code})

    # -- calls ---------------------------------------------------------------

    def create_call(self, agent_id: str, to_number: str, *, system_prompt: Optional[str] = None,
                    initial_greeting: Optional[str] = None, voice_id: Optional[str] = None) -> dict:
        return self.request("POST", "/v1/calls", body={
            "agent_id": agent_id, "to_number": to_number, "system_prompt": system_prompt,
            "initial_greeting": initial_greeting, "voice_id": voice_id})

    def get_call(self, call_id: str) -> dict:
        return self.request("GET", f"/v1/calls/{call_id}")

    def get_transcript(self, call_id: str) -> Any:
        return self.request("GET", f"/v1/calls/{call_id}/transcript")

    def list_calls(self, *, agent_id: Optional[str] = None, status: Optional[str] = None, limit: int = 10) -> list:
        return self.request("GET", "/v1/calls", params={"agent_id": agent_id, "status": status, "limit": limit})

    def hangup(self, call_id: str) -> dict:
        return self.request("POST", f"/v1/calls/{call_id}/hangup")

    def push_context(self, call_id: str, turn_id: Optional[str], push_token: Optional[str],
                     context: str, disposition: str) -> dict:
        """Answer a live caller turn over HTTP (the fallback when the relay socket is down)."""
        headers = {"X-Push-Token": push_token} if push_token else None
        return self.request("POST", f"/v1/calls/{call_id}/context", params={"turn_id": turn_id},
                            body={"context": context, "disposition": disposition}, headers=headers)

    # -- messages ------------------------------------------------------------

    def send_sms(self, agent_id: str, to_number: str, body: str) -> dict:
        return self.request("POST", "/v1/messages", body={"agent_id": agent_id, "to_number": to_number, "body": body})

    def list_messages(self, *, agent_id: Optional[str] = None, conversation_id: Optional[str] = None,
                      limit: int = 20) -> list:
        return self.request("GET", "/v1/messages",
                            params={"agent_id": agent_id, "conversation_id": conversation_id, "limit": limit})

    # -- contacts ------------------------------------------------------------

    def list_contacts(self, agent_id: str, *, q: Optional[str] = None, phone_number: Optional[str] = None,
                      limit: int = 50) -> dict:
        return self.request("GET", f"/v1/agents/{agent_id}/contacts",
                            params={"q": q, "phone_number": phone_number, "limit": limit})

    def create_contact(self, agent_id: str, name: str, phone_number: str, label: Optional[str] = None) -> dict:
        return self.request("POST", f"/v1/agents/{agent_id}/contacts",
                            body={"name": name, "phone_number": phone_number, "label": label})

    def update_contact(self, agent_id: str, contact_id: str, fields: dict) -> dict:
        return self.request("PATCH", f"/v1/agents/{agent_id}/contacts/{contact_id}", body=fields)


def resolve_agent_id(client: AgentLineClient, explicit: Optional[str] = None, configured: Optional[str] = None) -> str:
    """The agent to act as: an explicit id, then the configured one, then the account's only agent."""
    chosen = (explicit or configured or "").strip()
    if chosen:
        return chosen
    agents = client.list_agents()
    if len(agents) == 1:
        return agents[0]["id"]
    if not agents:
        raise AgentLineError(0, "this AgentLine account has no agent yet; create one with agentline_agent")
    names = ", ".join(f"{a.get('id')} ({a.get('name')})" for a in agents[:10])
    raise AgentLineError(0, f"several agents exist; pass agent_id or set default_agent_id. Agents: {names}")
