"""AgentLine gateway adapter: the phone line as a Hermes channel.

One outbound WebSocket to AgentLine's relay carries every inbound event, so no public URL or open
port is needed. Three events become agent turns:

* ``sms.received``     a text from an allowed number; the reply goes back as a text.
* ``call.utterance``   a question from a live caller; the reply is pushed into the call as facts
                       the hosted voice speaks. A turn that takes too long is answered by text.
* ``call.owner_task``  a task the owner gave by phone; the result is texted to the owner.

Senders are checked against ``AGENTLINE_ALLOWED_USERS`` here, before the gateway sees anything, so
a stranger who texts or calls the number never reaches the agent and never gets a reply.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import extra_or_secret, get_scoped_secret, seed_extra_from_env, send_error
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome
from gateway.platforms.helpers import MessageDeduplicator, redact_phone, strip_markdown

from . import relay, tools
from .client import AGENT_ID_ENV, API_KEY_ENV, WS_BASE, AgentLineClient, AgentLineError, resolve_agent_id
from .policy import normalize_number, number_list

logger = logging.getLogger(__name__)

PLATFORM = "agentline"
ALLOWED_USERS_ENV = "AGENTLINE_ALLOWED_USERS"
ALLOW_ALL_ENV = "AGENTLINE_ALLOW_ALL_USERS"
HOME_CHANNEL_ENV = "AGENTLINE_HOME_CHANNEL"

SMS_CHUNK_CHARS = 1500
SMS_MAX_CHUNKS = 3
TURN_TIMEOUT_SECONDS = 40.0
PING_SECONDS = 20.0
RECONNECT_BACKOFF = (1, 2, 5, 10, 30)
_TRUTHY = ("1", "true", "yes", "on")
_AUTH_FAILURE_STATUSES = (401, 403)

def _sms_chunks(text: str) -> list[str]:
    chunks = [text[i:i + SMS_CHUNK_CHARS] for i in range(0, len(text), SMS_CHUNK_CHARS)][:SMS_MAX_CHUNKS]
    if len(text) > SMS_CHUNK_CHARS * SMS_MAX_CHUNKS:
        chunks[-1] = chunks[-1][:-1] + "…"
    return chunks


def _http_status(error: BaseException) -> int:
    """The HTTP status of a refused WebSocket handshake, across ``websockets`` versions."""
    response = getattr(error, "response", None)
    return int(getattr(response, "status_code", None) or getattr(error, "status_code", None) or 0)


class AgentLineAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = SMS_CHUNK_CHARS

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform(PLATFORM))
        extra = config.extra or {}
        self._api_key: str = (get_scoped_secret(API_KEY_ENV, "") or "").strip()
        self._agent_id: str = extra_or_secret(extra, "agent_id", AGENT_ID_ENV)
        self._allow_all: bool = str(get_scoped_secret(ALLOW_ALL_ENV, "") or "").strip().lower() in _TRUTHY
        self._allowed: frozenset = frozenset(number_list(get_scoped_secret(ALLOWED_USERS_ENV, "") or ""))
        # People who texted or called in and were let through. Only matters with ALLOW_ALL_USERS (testing):
        # their replies are allowed, while every other unconfigured number is still refused.
        self._partners: set = set()
        self._client = AgentLineClient(self._api_key)
        self._owner_phone = ""
        self._socket: Any = None
        self._relay_task: Optional[asyncio.Task] = None
        self._turns: dict[str, relay.CallTurn] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._callers: dict[str, str] = {}
        self._dedup = MessageDeduplicator(max_size=2000, ttl_seconds=3600)

    # -- lifecycle -------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._api_key:
            logger.warning("[%s] %s is not set", self.name, API_KEY_ENV)
            return False
        try:
            self._agent_id = await asyncio.to_thread(resolve_agent_id, self._client, self._agent_id)
            agent = await asyncio.to_thread(self._client.get_agent, self._agent_id)
        except AgentLineError as exc:
            if exc.status in _AUTH_FAILURE_STATUSES:
                self._set_fatal_error("agentline_unauthorized", f"AgentLine rejected {API_KEY_ENV}.", retryable=False)
            logger.error("[%s] Cannot start: %s", self.name, exc)
            return False
        self._owner_phone = normalize_number(agent.get("owner_phone"))
        if not self._allow_all and not self._allowed:
            logger.warning("[%s] %s is empty: every inbound text and caller question is ignored",
                           self.name, ALLOWED_USERS_ENV)
        self._mark_connected()
        self._relay_task = asyncio.create_task(self._run_relay())
        self._wire_plugin_handlers(None)
        logger.info("[%s] Connected as agent %s", self.name, self._agent_id)
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        for task in (self._relay_task, *self._timers.values()):
            if task is not None:
                task.cancel()
        if self._relay_task is not None:
            await asyncio.gather(self._relay_task, return_exceptions=True)
        self._relay_task = None
        self._timers.clear()
        self._turns.clear()

    # -- relay socket ----------------------------------------------------------

    async def _run_relay(self) -> None:
        import websockets

        url = f"{WS_BASE}/v1/events/ws?agent_id={self._agent_id}&runtime=hermes"
        header_arg = ("additional_headers" if "additional_headers" in inspect.signature(websockets.connect).parameters
                      else "extra_headers")
        options = {"ping_interval": 20, "ping_timeout": 20, "max_size": 4 * 1024 * 1024,
                   header_arg: {"Authorization": f"Bearer {self._api_key}"}}
        attempt = 0
        while self._running:
            try:
                async with websockets.connect(url, **options) as socket:
                    self._socket, attempt = socket, 0
                    keepalive = asyncio.create_task(self._keepalive())
                    try:
                        async for raw in socket:
                            await self._on_frame(raw)
                    finally:
                        keepalive.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _http_status(exc) in _AUTH_FAILURE_STATUSES:
                    self._set_fatal_error("agentline_unauthorized",
                                          f"AgentLine relay rejected {API_KEY_ENV}.", retryable=False)
                    return
                logger.warning("[%s] Relay disconnected: %s", self.name, exc)
            finally:
                self._socket = None
            delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def _keepalive(self) -> None:
        """AgentLine drops a relay that sends no ``ping`` frame for 30 seconds."""
        while await self._send_frame({"type": "ping"}):
            await asyncio.sleep(PING_SECONDS)

    async def _send_frame(self, frame: dict) -> bool:
        socket = self._socket
        if socket is None:
            return False
        try:
            await socket.send(json.dumps(frame))
            return True
        except Exception as exc:
            logger.debug("[%s] Relay send failed: %s", self.name, exc)
            return False

    async def _on_frame(self, raw: Any) -> None:
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(frame, dict):
            return
        kind = frame.get("type")
        if kind == "heartbeat":
            await self._send_frame({"type": "ping"})
        elif kind == "cancel":
            self._cancel_turn(str(frame.get("turn_id") or ""))
        elif kind == "event":
            try:
                await self._on_event(frame)
            except Exception:
                logger.exception("[%s] Event %s failed", self.name, frame.get("event_id"))

    # -- inbound events --------------------------------------------------------

    def _is_allowed(self, number: str) -> bool:
        return self._allow_all or (bool(number) and number in self._allowed)

    async def _on_event(self, event: dict) -> None:
        event_id = str(event.get("event_id") or "")
        event_type = event.get("event_type")
        if self._dedup.is_duplicate(event_id):
            if event_type != "call.utterance":  # an open turn acks when it settles
                await self._send_frame(relay.ack_frame(event_id))
            return
        if event_type == "call.utterance":
            await self._on_utterance(event)
            return
        if event_type == "sms.received":
            await self._on_sms(event)
        elif event_type == "call.owner_task":
            await self._on_owner_task(event)
        await self._send_frame(relay.ack_frame(event_id))

    async def _dispatch(self, *, chat_id: str, sender: str, text: str, message_id: str, raw: dict) -> None:
        source = self.build_source(chat_id=chat_id, chat_name=sender, chat_type="dm", user_id=sender,
                                   user_name=sender, message_id=message_id)
        await self.handle_message(MessageEvent(
            text=text, message_type=MessageType.TEXT, source=source, message_id=message_id,
            raw_message=raw, timestamp=datetime.now(tz=timezone.utc)))

    async def _on_sms(self, event: dict) -> None:
        sms = relay.parse_sms(event)
        if sms is None:
            return
        if not self._is_allowed(sms.from_number):
            logger.info("[%s] Ignored a text from %s (not in %s)", self.name,
                        redact_phone(sms.from_number), ALLOWED_USERS_ENV)
            return
        self._partners.add(sms.from_number)
        await self._dispatch(chat_id=sms.from_number, sender=sms.from_number, text=sms.body,
                             message_id=sms.message_id, raw=event)

    async def _on_owner_task(self, event: dict) -> None:
        text = relay.owner_task_text(event)
        if not text or not self._owner_phone or not self._is_allowed(self._owner_phone):
            return
        await self._dispatch(chat_id=self._owner_phone, sender=self._owner_phone, text=text,
                             message_id=str(event.get("event_id") or ""), raw=event)

    async def _caller_of(self, turn: relay.CallTurn) -> str:
        if turn.caller:
            self._callers[turn.call_id] = turn.caller
        elif turn.call_id not in self._callers:
            try:
                call = await asyncio.to_thread(self._client.get_call, turn.call_id)
            except AgentLineError as exc:
                logger.debug("[%s] Could not look up call %s: %s", self.name, turn.call_id, exc)
                call = {}
            self._callers[turn.call_id] = relay.call_counterpart(call)
            if len(self._callers) > 500:
                self._callers.pop(next(iter(self._callers)))
        return self._callers.get(turn.call_id, "")

    async def _on_utterance(self, event: dict) -> None:
        turn = relay.parse_call_turn(event)
        event_id = str(event.get("event_id") or "")
        if turn is None:
            await self._send_frame(relay.ack_frame(event_id))
            return
        turn.caller = await self._caller_of(turn)
        if not self._is_allowed(turn.caller):
            logger.info("[%s] Refused a live question from %s (not in %s)", self.name,
                        redact_phone(turn.caller), ALLOWED_USERS_ENV)
            await self._settle(turn, "failed", "I can't look that up for this caller.")
            return
        if turn.caller:
            self._partners.add(turn.caller)
        previous = self._turns.get(turn.call_id)
        if previous is not None and not previous.settled:
            await self._settle(previous, None, "")  # the caller moved on to a new question
        self._turns[turn.call_id] = turn
        self._timers[turn.turn_id] = asyncio.create_task(self._expire(turn))
        await self._dispatch(chat_id=turn.chat_id, sender=turn.caller or turn.chat_id,
                             text=relay.turn_prompt(turn), message_id=turn.turn_id, raw=event)

    # -- live call turns -------------------------------------------------------

    async def _push(self, turn: relay.CallTurn, context: str, disposition: str) -> None:
        if await self._send_frame(relay.context_frame(turn, context, disposition)):
            return
        try:
            await asyncio.to_thread(self._client.push_context, turn.call_id, turn.turn_id,
                                    turn.push_token, context, disposition)
        except AgentLineError as exc:  # 409 stale turn, 410 call ended: nothing left to answer
            logger.info("[%s] Could not answer turn %s: %s", self.name, turn.turn_id, exc)

    async def _settle(self, turn: relay.CallTurn, disposition: Optional[str], context: str) -> None:
        """Close a turn once: push the answer (``disposition=None`` pushes nothing) and ack the event."""
        if turn.settled:
            return
        turn.settled = True
        timer = self._timers.pop(turn.turn_id, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        if disposition:
            await self._push(turn, context, disposition)
        await self._send_frame(relay.ack_frame(turn.event_id))

    async def _expire(self, turn: relay.CallTurn) -> None:
        await asyncio.sleep(TURN_TIMEOUT_SECONDS)
        if turn.settled:
            return
        turn.timed_out = True
        note = ("This is taking longer than a call allows. The answer will arrive by text message."
                if turn.caller else "I could not get that in time.")
        await self._settle(turn, "failed", note)

    def _cancel_turn(self, turn_id: str) -> None:
        for turn in self._turns.values():
            if turn.turn_id == turn_id and not turn.settled:
                asyncio.ensure_future(self._settle(turn, None, ""))

    def _turn_for_event(self, event: MessageEvent) -> Optional[relay.CallTurn]:
        chat_id = str(getattr(event.source, "chat_id", "") or "")
        if not chat_id.startswith(relay.CALL_CHAT_PREFIX):
            return None
        turn = self._turns.get(chat_id[len(relay.CALL_CHAT_PREFIX):])
        return turn if turn is not None and turn.turn_id == event.message_id else None

    async def on_processing_start(self, event: MessageEvent) -> None:
        turn = self._turn_for_event(event)
        if turn is not None and not turn.settled:
            await self._push(turn, "Looking that up now.", "progress")

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        turn = self._turn_for_event(event)
        if turn is None:
            return
        answer = " ".join(part for part in turn.parts if part).strip()
        if turn.timed_out:
            if answer and turn.caller and outcome is ProcessingOutcome.SUCCESS:
                await self._send_sms(turn.caller, f"From your call: {answer}")
        elif outcome is ProcessingOutcome.SUCCESS and answer:
            await self._settle(turn, "done", answer)
        elif outcome is ProcessingOutcome.CANCELLED:
            await self._settle(turn, "noop", "")
        else:
            await self._settle(turn, "failed", "I could not get that right now.")
        if self._turns.get(turn.call_id) is turn:
            del self._turns[turn.call_id]

    # -- outbound --------------------------------------------------------------

    def format_message(self, content: str) -> str:
        return strip_markdown(content)

    def _trusted(self) -> frozenset:
        home = (getattr(self.config, "home_channel", None) or {})
        home_id = home.get("chat_id") if isinstance(home, dict) else getattr(home, "chat_id", None)
        return tools.trusted_numbers(sorted(self._allowed), sorted(self._partners), home_id or "",
                                     read_secret=lambda name: get_scoped_secret(name, "") or "")

    async def _send_sms(self, number: str, text: str) -> SendResult:
        """Every text the gateway sends: replies, ``send_message``, cron delivery and late call answers.

        These paths can run unattended (a scheduled job delivers here at fire time), so they only text
        numbers the user configured, or someone already talking to the agent; an empty allowlist means
        deny. Every text counts against ``daily_sms_limit``."""
        problem = tools.text_problem(number, self._trusted())
        chunks = _sms_chunks(text)
        problem = problem or tools.sms_budget_problem(len(chunks))
        if problem:
            logger.warning("[%s] Refused a text to %s: %s", self.name, redact_phone(number), problem)
            return SendResult(success=False, error=f"AgentLine: {problem}")
        message_id = None
        try:
            for chunk in chunks:
                sent = await asyncio.to_thread(self._client.send_sms, self._agent_id, number, chunk)
                tools.count_sms(1)
                message_id = sent.get("id") or message_id
        except AgentLineError as exc:
            logger.warning("[%s] Text to %s failed: %s", self.name, redact_phone(number), exc)
            return SendResult(success=False, error=str(exc), retryable=exc.status == 0)
        return SendResult(success=True, message_id=message_id)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        text = self.format_message(content or "").strip()
        if not text:
            return SendResult(success=True)
        if chat_id.startswith(relay.CALL_CHAT_PREFIX):
            turn = self._turns.get(chat_id[len(relay.CALL_CHAT_PREFIX):])
            if turn is None:
                return SendResult(success=False, error="the call has no open question to answer")
            # Only the agent's reply is spoken. The gateway marks it ``notify``; setup notices and
            # other side messages sent to this chat are not for the caller.
            if (metadata or {}).get("notify"):
                turn.parts.append(text)  # delivered when the turn completes
            return SendResult(success=True, message_id=turn.turn_id)
        number = normalize_number(chat_id)
        if not number:
            return SendResult(success=False, error=f"not a US or Canadian phone number: {chat_id}")
        return await self._send_sms(number, text)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id, "type": "dm"}


# -- registration ----------------------------------------------------------------


def check_requirements() -> bool:
    """Passive probe: the relay library is importable and an API key is configured."""
    return importlib.util.find_spec("websockets") is not None and bool(
        (get_scoped_secret(API_KEY_ENV, "", external_fallback=True) or "").strip())


def validate_config(config) -> bool:
    return bool((get_scoped_secret(API_KEY_ENV, "") or "").strip())


def _env_enablement() -> Optional[dict]:
    if not (get_scoped_secret(API_KEY_ENV, "") or "").strip():
        return None
    return seed_extra_from_env(((AGENT_ID_ENV, "agent_id", None),), home_env=HOME_CHANNEL_ENV)


async def _standalone_send(pconfig, chat_id, message, *, thread_id=None, media_files=None, force_document=False):
    """Text delivery for cron jobs that run outside the gateway process."""
    api_key = (get_scoped_secret(API_KEY_ENV, "") or "").strip()
    number = normalize_number(chat_id)
    if not api_key or not number:
        return send_error(f"AgentLine text needs {API_KEY_ENV} and a US or Canadian number")
    home = getattr(pconfig, "home_channel", None) or {}
    home_id = home.get("chat_id") if isinstance(home, dict) else getattr(home, "chat_id", None)
    trusted = tools.trusted_numbers(home_id or "", read_secret=lambda name: get_scoped_secret(name, "") or "")
    chunks = _sms_chunks(strip_markdown(message))
    problem = tools.text_problem(number, trusted) or tools.sms_budget_problem(len(chunks))
    if problem:
        return send_error(f"AgentLine: {problem}")
    client = AgentLineClient(api_key)
    extra = getattr(pconfig, "extra", {}) or {}
    try:
        agent_id = await asyncio.to_thread(resolve_agent_id, client, extra_or_secret(extra, "agent_id", AGENT_ID_ENV))
        sent: dict = {}
        for chunk in chunks:
            sent = await asyncio.to_thread(client.send_sms, agent_id, number, chunk)
            tools.count_sms(1)
    except AgentLineError as exc:
        return send_error(f"AgentLine text failed: {exc}")
    return {"success": True, "platform": PLATFORM, "chat_id": number, "message_id": sent.get("id")}
