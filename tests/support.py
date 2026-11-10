"""Test support: load the plugin as a package and stand in for the Hermes modules it imports.

The stand-ins implement only what the plugin uses, with the same names and signatures as the
public Hermes plugin API, so the tests run with the standard library alone.
"""

from __future__ import annotations

import enum
import importlib.util
import sys
import tempfile
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PACKAGE = "agentline_plugin"
SECRETS: dict[str, str] = {}
DATA_DIR = Path(tempfile.mkdtemp(prefix="agentline-tests-"))


def _module(name: str, **members: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(members)
    sys.modules[name] = module
    return module


@dataclass
class SendResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    retryable: bool = False


@dataclass
class PlatformConfig:
    extra: dict = field(default_factory=dict)


@dataclass
class Source:
    chat_id: str
    user_id: Optional[str] = None
    message_id: Optional[str] = None


class MessageType(enum.Enum):
    TEXT = "text"


class ProcessingOutcome(enum.Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass
class MessageEvent:
    text: str
    message_type: MessageType = MessageType.TEXT
    source: Any = None
    raw_message: Any = None
    message_id: Optional[str] = None
    timestamp: Any = None


class BasePlatformAdapter:
    def __init__(self, config, platform):
        self.config, self.platform, self.name = config, platform, str(platform)
        self._running = False
        self.fatal: Optional[str] = None
        self.inbound: list[MessageEvent] = []

    def _mark_connected(self):
        self._running = True

    def _mark_disconnected(self):
        self._running = False

    def _set_fatal_error(self, code, message, *, retryable):
        self._running, self.fatal = False, code

    def _wire_plugin_handlers(self, native=None):
        pass

    def build_source(self, chat_id, chat_name=None, chat_type="dm", user_id=None, user_name=None, message_id=None):
        return Source(chat_id=chat_id, user_id=user_id, message_id=message_id)

    async def handle_message(self, event):
        self.inbound.append(event)


class MessageDeduplicator:
    def __init__(self, max_size=2000, ttl_seconds=300):
        self._seen: set = set()

    def is_duplicate(self, msg_id):
        if not msg_id or msg_id in self._seen:
            return bool(msg_id)
        self._seen.add(msg_id)
        return False


@dataclass
class PlatformEntry:
    name: str = ""
    label: str = ""
    adapter_factory: Any = None
    check_fn: Any = None
    allowlist_suppresses_pairing: bool = False
    display_tier: str = ""


def _get_secret(name, default=None, **kwargs):
    return SECRETS.get(name, default)


def _extra_or_secret(extra, key, env, default=""):
    return SECRETS.get(env) or (extra or {}).get(key) or default


def install_stubs() -> None:
    _module("agent")
    _module("agent.secret_scope", get_secret=_get_secret)
    _module("plugins")
    _module("plugins.plugin_storage", plugin_data_dir=lambda name: DATA_DIR)
    _module("gateway")
    _module("gateway.config", Platform=str, PlatformConfig=PlatformConfig)
    _module("gateway.platform_registry", PlatformEntry=PlatformEntry)
    _module("gateway.platforms")
    _module("gateway.platforms._shared", extra_or_secret=_extra_or_secret, get_scoped_secret=_get_secret,
            seed_extra_from_env=lambda spec, home_env=None: {}, send_error=lambda m: {"error": str(m)})
    _module("gateway.platforms.base", BasePlatformAdapter=BasePlatformAdapter, SendResult=SendResult)
    _module("gateway.platforms.event", MessageEvent=MessageEvent, MessageType=MessageType,
            ProcessingOutcome=ProcessingOutcome)
    _module("gateway.platforms.helpers", MessageDeduplicator=MessageDeduplicator,
            redact_phone=lambda phone: (phone or "")[:4] + "****",
            strip_markdown=lambda text: text.replace("**", "").replace("`", ""))


def load_plugin() -> types.ModuleType:
    """Import the plugin directory as the package ``agentline_plugin`` (once)."""
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    install_stubs()
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def submodule(name: str) -> types.ModuleType:
    load_plugin()
    return importlib.import_module(f"{PACKAGE}.{name}")


class FakeClient:
    """Records AgentLine calls and returns canned answers instead of using the network."""

    def __init__(self, **answers: Any):
        self.calls: list[tuple] = []
        self.answers = answers

    def __getattr__(self, method: str):
        def call(*args, **kwargs):
            self.calls.append((method, args, kwargs))
            answer = self.answers.get(method, {})
            if isinstance(answer, Exception):
                raise answer
            if isinstance(answer, list) and answer and isinstance(answer[0], _Sequence):
                return answer[0].next()
            return answer

        return call

    def named(self, method: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == method]


class _Sequence:
    def __init__(self, *values):
        self.values = list(values)

    def next(self):
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


def sequence(*values) -> list:
    """An answer that changes on each call; the last value repeats."""
    return [_Sequence(*values)]
