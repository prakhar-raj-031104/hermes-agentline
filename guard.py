"""``pre_tool_call`` hook: every action that costs money or reaches a real person asks first.

A blocked destination (emergency code, premium rate, not on the allowlist) is refused outright.
Anything else goes to Hermes's own approval gate, which fails closed when nobody answers, so an
unattended run (cron, a gateway with no approver) never dials by itself.
"""

from __future__ import annotations

from typing import Any, Optional

from .policy import destination_problem, normalize_number
from .tools import setting

_REACHES_A_PERSON = {
    "agentline_call": "Place a phone call to {number}",
    "agentline_sms": "Send a text message to {number}",
}
_SPENDS_MONEY = {"agentline_buy_number": "Buy a phone number on your AgentLine account ($3.00 every 30 days)"}


def _preview(text: Any, limit: int = 240) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **kwargs: Any) -> Optional[dict]:
    args = args or {}
    if tool_name in _SPENDS_MONEY:
        if not setting("require_approval"):
            return None
        return {"action": "approve", "message": _SPENDS_MONEY[tool_name], "rule_key": f"agentline:{tool_name}"}
    if tool_name not in _REACHES_A_PERSON:
        return None
    problem = destination_problem(args.get("to_number"), setting("allowed_numbers"))
    if problem:
        return {"action": "block", "message": f"AgentLine: {problem}."}
    if not setting("require_approval"):
        return None
    number = normalize_number(args.get("to_number"))
    detail = args.get("objective") if tool_name == "agentline_call" else args.get("body")
    message = _REACHES_A_PERSON[tool_name].format(number=number)
    if detail:
        message += f": {_preview(detail)}"
    return {"action": "approve", "message": message, "rule_key": f"agentline:{tool_name}:{number}"}
