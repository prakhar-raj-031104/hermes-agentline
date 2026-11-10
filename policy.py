"""Destination rules shared by the approval hook and the tool handlers.

AgentLine numbers reach the US and Canada, so a destination must be a +1 number. Emergency and
service codes and premium-rate numbers are always refused, whatever the settings say.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

_NANP_RE = re.compile(r"^\+1([2-9]\d{2})([2-9]\d{2})(\d{4})$")
_SEPARATORS_RE = re.compile(r"[\s().\-]")
_PREMIUM_AREA_CODES = frozenset({"900"})
_PREMIUM_EXCHANGES = frozenset({"976"})


def normalize_number(raw: object) -> str:
    """``+12125551234`` for a US/Canada number written with or without +1; ``""`` when it is not one."""
    text = _SEPARATORS_RE.sub("", str(raw or ""))
    if not text:
        return ""
    if text.startswith("+"):
        digits = text[1:]
    elif len(text) == 10:
        digits = "1" + text
    else:
        digits = text
    candidate = "+" + digits
    return candidate if digits.isdigit() and _NANP_RE.match(candidate) else ""


def number_list(raw: object) -> list[str]:
    """Normalized numbers from a list or a comma-separated string; entries that are not numbers are dropped."""
    items: Iterable = raw.split(",") if isinstance(raw, str) else (raw or [])
    return [n for n in (normalize_number(item) for item in items) if n]


def destination_problem(raw: object, allowed_numbers: object = None) -> Optional[str]:
    """Why this destination may not be called or texted, or ``None`` when it may."""
    text = _SEPARATORS_RE.sub("", str(raw or ""))
    if re.fullmatch(r"\+?1?[2-9]11", text):
        return "emergency and service codes (911, 411, ...) cannot be called or texted"
    number = normalize_number(raw)
    if not number:
        return "the number must be a US or Canadian number in E.164 form, for example +12125551234"
    area, exchange, _ = _NANP_RE.match(number).groups()
    if area in _PREMIUM_AREA_CODES or exchange in _PREMIUM_EXCHANGES:
        return "premium-rate numbers are blocked"
    allowed = number_list(allowed_numbers)
    if allowed and number not in allowed:
        return "this number is not on the plugin's allowed_numbers list"
    return None
