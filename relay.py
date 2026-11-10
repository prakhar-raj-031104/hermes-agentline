"""AgentLine relay events turned into plain data. No Hermes imports, so this is testable alone.

The relay socket delivers ``{"type": "event", "event_id", "agent_id", "event_type", "payload"}``
frames. Three event types become agent turns: an inbound SMS, a live caller question
(``call.utterance``) and a task the owner gave by phone (``call.owner_task``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .policy import normalize_number

CALL_CHAT_PREFIX = "call:"
TRANSCRIPT_TAIL = 12
_CALLER_ROLES = frozenset({"user", "human", "caller"})


def _first(payload: dict, *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


@dataclass
class InboundSms:
    event_id: str
    from_number: str
    body: str
    message_id: str


@dataclass
class CallTurn:
    """One open caller question on a live call."""

    event_id: str
    call_id: str
    turn_id: str
    push_token: str
    utterance: str
    caller: str = ""
    lines: list = field(default_factory=list)
    parts: list = field(default_factory=list)
    settled: bool = False
    timed_out: bool = False

    @property
    def chat_id(self) -> str:
        return CALL_CHAT_PREFIX + self.call_id


def parse_sms(event: dict) -> Optional[InboundSms]:
    payload = event.get("payload") or {}
    sender = normalize_number(_first(payload, "from_number", "from", "sender", "caller_number"))
    body = str(_first(payload, "body", "text", "message") or "").strip()
    if not sender or not body:
        return None
    event_id = str(event.get("event_id") or "")
    return InboundSms(event_id=event_id, from_number=sender, body=body,
                      message_id=str(_first(payload, "message_id", "id") or event_id))


def payload_caller(payload: dict) -> str:
    """The other party's number when the event names it; ``""`` otherwise (the adapter then asks the API)."""
    caller = payload.get("caller")
    if isinstance(caller, dict):
        caller = _first(caller, "phone_number", "number", "from_number")
    return normalize_number(caller or _first(payload, "caller_number", "from_number", "from"))


def call_counterpart(call: dict) -> str:
    """The human's number from a call record: who called in, or who was dialed."""
    key = "to_number" if str(call.get("direction") or "").lower() == "outbound" else "from_number"
    return normalize_number(call.get(key))


def parse_call_turn(event: dict) -> Optional[CallTurn]:
    payload = event.get("payload") or {}
    call_id = str(payload.get("call_id") or "")
    turn_id = str(payload.get("turn_id") or "")
    utterance = str(_first(payload, "utterance", "ask", "text") or "").strip()
    if not call_id or not turn_id or not utterance:
        return None
    return CallTurn(
        event_id=str(event.get("event_id") or ""), call_id=call_id, turn_id=turn_id,
        push_token=str(payload.get("push_token") or ""), utterance=utterance,
        caller=payload_caller(payload),
        lines=list(_first(payload, "transcript", "conversation") or []))


def _line_text(line: Any) -> str:
    if not isinstance(line, dict):
        return str(line)
    speaker = "caller" if str(line.get("role") or "").lower() in _CALLER_ROLES else "voice"
    return f"{speaker}: {_first(line, 'content', 'text', 'message') or ''}"


def turn_prompt(turn: CallTurn) -> str:
    """The message the agent sees for a live caller question."""
    who = turn.caller or "the caller"
    lines = [
        f"[Live phone call. {who} is on the line with your phone voice and is waiting for this.]",
        f"Request: {turn.utterance}",
        "Reply with only the short facts or the result, in one to three plain sentences. "
        "The phone voice says them in its own words, so do not greet, do not ask the caller to hold "
        "and do not ask a question unless you cannot proceed without the answer.",
    ]
    tail = turn.lines[-TRANSCRIPT_TAIL:]
    if tail:
        lines.append("Call so far:")
        lines.extend(_line_text(line) for line in tail)
    return "\n".join(lines)


def owner_task_text(event: dict) -> str:
    """The instructions the owner spoke on a task call, as one message."""
    payload = event.get("payload") or {}
    task = _first(payload, "task", "instructions", "summary")
    if not task:
        spoken = [str(_first(line, "content", "text", "message") or "")
                  for line in (_first(payload, "transcript", "conversation") or [])
                  if isinstance(line, dict) and str(line.get("role") or "").lower() in _CALLER_ROLES]
        task = " ".join(part for part in spoken if part)
    task = str(task or "").strip()
    if not task:
        return ""
    return f"[Task you gave by phone call]\n{task}"


def context_frame(turn: CallTurn, context: str, disposition: str) -> dict:
    return {"type": "context", "event_id": turn.event_id, "call_id": turn.call_id, "turn_id": turn.turn_id,
            "push_token": turn.push_token, "context": context, "disposition": disposition}


def ack_frame(event_id: str) -> dict:
    return {"type": "ack", "event_id": event_id}
