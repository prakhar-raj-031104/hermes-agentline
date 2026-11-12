"""``pre_tool_call`` hook: every action that costs money or reaches a real person asks first.

* ``agentline_call``, ``agentline_sms`` and ``agentline_buy_number`` go to Hermes's approval gate,
  with the full call brief, greeting, voice or message body in the prompt.
* ``agentline_agent`` changes what every caller hears, so it asks first as well.
* Hermes's own ``send_message`` tool can reach the phone line with a target like
  ``agentline:+14155551234``. Numbers the user trusts (AGENTLINE_ALLOWED_USERS, the home channel,
  ``allowed_numbers``) are texted without a prompt; any other number needs approval, and a
  scheduled (cron) run is refused outright, since nobody is there to approve it.

Emergency codes, premium-rate numbers and numbers outside the US and Canada are refused on every
path. Hermes's approval gate fails closed when nobody can answer.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Optional

from .policy import destination_problem, normalize_number
from .tools import MAX_GREETING_CHARS, MAX_OBJECTIVE_CHARS, setting, text_problem, trusted_numbers

PLATFORM_PREFIX = "agentline"
_AGENT_FIELDS = ("name", "system_prompt", "initial_greeting", "voice_id", "voicemail_message", "owner_phone")

_current_cron: Callable[[], Any] = lambda: None


def bind_context(ctx) -> None:
    """Remember how to ask Hermes whether the current code runs inside a scheduled (cron) run."""
    global _current_cron
    lookup = getattr(ctx, "current_cron_execution", None)
    if callable(lookup):
        _current_cron = lookup


def _in_cron() -> bool:
    try:
        return _current_cron() is not None
    except Exception:  # unknown means unattended: refuse rather than risk an unapproved text
        return True


def _rule_key(tool_name: str, content: str) -> str:
    """Approve-for-session and always-allow remember this exact content only, never a whole tool."""
    return f"agentline:{tool_name}:{hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]}"


def _approve(tool_name: str, message: str) -> dict:
    return {"action": "approve", "message": message, "rule_key": _rule_key(tool_name, message)}


def _block(reason: str) -> dict:
    return {"action": "block", "message": f"AgentLine: {reason}."}


def _call_directive(args: dict) -> Optional[dict]:
    problem = destination_problem(args.get("to_number"), setting("allowed_numbers"))
    if problem:
        return _block(problem)
    objective = str(args.get("objective") or "").strip()
    greeting = str(args.get("greeting") or "").strip()
    if len(objective) > MAX_OBJECTIVE_CHARS:
        return _block(f"the call brief is {len(objective)} characters; the limit is {MAX_OBJECTIVE_CHARS}")
    if len(greeting) > MAX_GREETING_CHARS:
        return _block(f"the greeting is longer than {MAX_GREETING_CHARS} characters")
    if not setting("require_approval"):
        return None
    lines = [f"Place a phone call to {normalize_number(args.get('to_number'))}",
             f"Opening line: {greeting or '(the agent default)'}",
             f"Voice: {args.get('voice_id') or '(the agent default)'}",
             "Brief for the voice:", objective or "(empty)"]
    return _approve("agentline_call", "\n".join(lines))


def _sms_directive(args: dict) -> Optional[dict]:
    problem = destination_problem(args.get("to_number"), setting("allowed_numbers"))
    if problem:
        return _block(problem)
    if not setting("require_approval"):
        return None
    number = normalize_number(args.get("to_number"))
    return _approve("agentline_sms", f"Send a text message to {number}:\n{str(args.get('body') or '')}")


def _agent_directive(args: dict) -> Optional[dict]:
    changes = {key: args.get(key) for key in _AGENT_FIELDS if args.get(key)}
    if not changes or not setting("require_approval"):
        return None
    verb = f"Change AgentLine agent {args['agent_id']}" if args.get("agent_id") else "Create an AgentLine agent"
    lines = [f"{verb}. This is what every caller will hear:"]
    lines += [f"{key}: {value}" for key, value in changes.items()]
    return _approve("agentline_agent", "\n".join(lines))


def _send_message_directive(args: dict) -> Optional[dict]:
    """Hermes's ``send_message`` with an ``agentline:`` target texts a real phone number."""
    platform, _, ref = str(args.get("target") or "").strip().partition(":")
    if platform.strip().lower() != PLATFORM_PREFIX:
        return None
    ref = ref.strip()
    if not ref:
        return None  # the home channel, which the user configured
    number = normalize_number(ref)
    if not number:
        return _block("send_message can only text a US or Canadian number, for example agentline:+14155551234")
    trusted = trusted_numbers()
    problem = text_problem(number, trusted)
    if problem:
        return _block(problem)
    if number in trusted:
        return None
    if _in_cron():
        return _block("a scheduled run cannot text a number outside AGENTLINE_ALLOWED_USERS, the home "
                      "channel and allowed_numbers, because nobody is there to approve it")
    if not setting("require_approval"):
        return None
    return _approve("send_message", f"Send a text message to {number}, which is not on your AgentLine "
                                    f"allowlist:\n{str(args.get('message') or '')}")


_DIRECTIVES = {
    "agentline_call": _call_directive,
    "agentline_sms": _sms_directive,
    "agentline_agent": _agent_directive,
    "send_message": _send_message_directive,
}


def pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **kwargs: Any) -> Optional[dict]:
    args = args or {}
    if tool_name == "agentline_buy_number":
        if not setting("require_approval"):
            return None
        return {"action": "approve", "message": "Buy a phone number on your AgentLine account ($3.00 every 30 days)",
                "rule_key": "agentline:agentline_buy_number"}
    handler = _DIRECTIVES.get(tool_name)
    return handler(args) if handler else None
