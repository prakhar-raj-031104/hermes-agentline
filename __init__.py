"""AgentLine for Hermes: a real phone number for your agent.

``register(ctx)`` wires four things through the public plugin API:

* nine ``agentline_*`` tools (calls, SMS, contacts, setup),
* a ``pre_tool_call`` hook that sends every call, text and number purchase to the approval gate,
* the ``agentline`` gateway platform, so you can text or phone your agent,
* a bundled skill on how to brief a phone call.

The gateway adapter is imported only when the gateway asks for it, so CLI startup stays cheap.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from . import guard, tools

__all__ = ["register"]

PLATFORM = "agentline"
API_KEY_ENV = "AGENTLINE_API_KEY"
ALLOWED_USERS_ENV = "AGENTLINE_ALLOWED_USERS"
ALLOW_ALL_ENV = "AGENTLINE_ALLOW_ALL_USERS"
HOME_CHANNEL_ENV = "AGENTLINE_HOME_CHANNEL"


def _adapter():
    from . import adapter

    return adapter


def _make_adapter(config):
    return _adapter().AgentLineAdapter(config)


def _check_requirements() -> bool:
    return _adapter().check_requirements()


def _validate_config(config) -> bool:
    return _adapter().validate_config(config)


def _env_enablement():
    return _adapter()._env_enablement()


async def _standalone_send(pconfig, chat_id, message, **kwargs):
    return await _adapter()._standalone_send(pconfig, chat_id, message, **kwargs)


def _register_platform(ctx) -> None:
    from gateway.platform_registry import PlatformEntry

    kwargs: dict[str, Any] = dict(
        name=PLATFORM, label="AgentLine (phone)", adapter_factory=_make_adapter,
        check_fn=_check_requirements, validate_config=_validate_config,
        required_env=[API_KEY_ENV],
        install_hint="Enable the agentline plugin so its websockets dependency installs.",
        env_enablement_fn=_env_enablement, cron_deliver_env_var=HOME_CHANNEL_ENV,
        standalone_sender_fn=_standalone_send,
        allowed_users_env=ALLOWED_USERS_ENV, allow_all_env=ALLOW_ALL_ENV,
        max_message_length=1500, emoji="📞",
        platform_hint=(
            "You are reachable on a real phone line (AgentLine). Messages arrive as SMS, so reply in "
            "short plain text with no markdown; long replies are split into several texts and cost "
            "more. A message that starts with [Live phone call ...] is a question from someone who is "
            "on a call right now: answer with one to three short factual sentences, which a voice "
            "then says aloud."
        ),
    )
    known = {field.name for field in dataclasses.fields(PlatformEntry)}
    for key, value in (
        ("allowlist_suppresses_pairing", True),  # never text a pairing code to a stranger
        ("display_tier", "minimal"),             # no tool-progress texts: each one is a billed SMS
        ("dashboard_description", "Text and call Hermes on a real phone number (AgentLine)."),
        ("docs_url", "https://docs.agentline.cloud"),
    ):
        if key in known:  # older cores reject unknown register_platform kwargs
            kwargs[key] = value
    ctx.register_platform(**kwargs)


def register(ctx) -> None:
    tools.bind_settings(ctx.get_config)
    tools.register_tools(ctx)
    ctx.register_hook("pre_tool_call", guard.pre_tool_call)
    skill = Path(__file__).parent / "skills" / "agentline-phone" / "SKILL.md"
    ctx.register_skill("agentline-phone", skill)
    _register_platform(ctx)
