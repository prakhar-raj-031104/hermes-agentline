"""``pre_tool_call`` hook: every action that costs money or reaches a real person asks first.

* ``agentline_call``, ``agentline_sms`` and ``agentline_buy_number`` go to Hermes's approval gate,
  with the full call brief, greeting, voice or message body in the prompt.
* ``agentline_agent`` changes what every caller hears, so it asks first as well.
* Hermes's own ``send_message`` tool and scheduled jobs (``cronjob_manage`` with
  ``deliver``/``failure_deliver`` set to ``agentline:+1...``) can text through the phone line.
  Those texts only go to numbers the user configured (AGENTLINE_ALLOWED_USERS, the home channel,
  ``allowed_numbers``); any other number is refused here, and again in the gateway when the text
  is sent, so a job can never be scheduled to text a stranger.
* Inside a scheduled run, ``agentline_call`` and ``agentline_sms`` are refused for numbers the
  user did not configure, since nobody is there to approve them.

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


def _unattended_problem(to_number: object) -> Optional[str]:
    """Inside a scheduled run nobody can approve, so only configured numbers may be reached."""
    if _in_cron() and normalize_number(to_number) not in trusted_numbers():
        return ("a scheduled run cannot call or text a number outside AGENTLINE_ALLOWED_USERS, the "
                "home channel and allowed_numbers, because nobody is there to approve it")
    return None


def _call_directive(args: dict) -> Optional[dict]:
    problem = destination_problem(args.get("to_number"), setting("allowed_numbers")) \
        or _unattended_problem(args.get("to_number"))
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
    problem = destination_problem(args.get("to_number"), setting("allowed_numbers")) \
        or _unattended_problem(args.get("to_number"))
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


def _agentline_target_problem(target: object) -> Optional[str]:
    """Why an ``agentline:...`` delivery target may not be used, ``None`` when it may, or when the
    target belongs to another platform. A bare ``agentline`` means the home channel."""
    platform, _, ref = str(target or "").strip().partition(":")
    if platform.strip().lower() != PLATFORM_PREFIX:
        return None
    ref = ref.strip().split(":", 1)[0].strip()   # agentline:<number>[:<thread>]
    if not ref:
        return None
    number = normalize_number(ref)
    if not number:
        return "only a US or Canadian number can be texted, for example agentline:+14155551234"
    return text_problem(number, trusted_numbers())


def _send_message_directive(args: dict) -> Optional[dict]:
    """Hermes's ``send_message`` with an ``agentline:`` target texts a real phone number."""
    problem = _agentline_target_problem(args.get("target"))
    return _block(f"send_message: {problem}") if problem else None


def _cron_directive(args: dict) -> Optional[dict]:
    """A scheduled job that delivers to ``agentline:<number>`` texts it at fire time, unattended."""
    if str(args.get("action") or "").strip().lower() not in ("create", "update"):
        return None
    for field in ("deliver", "failure_deliver"):
        value = args.get(field)
        parts = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
        for part in parts:
            problem = _agentline_target_problem(part)
            if problem:
                return _block(f"cron job {field} {str(part).strip()}: {problem}")
    return None


_DIRECTIVES = {
    "agentline_call": _call_directive,
    "agentline_sms": _sms_directive,
    "agentline_agent": _agent_directive,
    "send_message": _send_message_directive,
    "cronjob_manage": _cron_directive,
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
