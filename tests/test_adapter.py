import asyncio
import json
import unittest

from tests import support
from tests.support import FakeClient, MessageEvent, PlatformConfig, ProcessingOutcome, Source

adapter_mod = support.submodule("adapter")
relay = support.submodule("relay")
client_mod = support.submodule("client")
tools_mod = support.submodule("tools")

FINAL = {"notify": True}  # how the gateway marks the agent's reply
OWNER = "+14155550123"
STRANGER = "+12125550199"


class FakeSocket:
    def __init__(self):
        self.frames: list[dict] = []

    async def send(self, raw):
        self.frames.append(json.loads(raw))

    def of(self, kind):
        return [f for f in self.frames if f["type"] == kind]


def event(event_type, event_id="evt_1", **payload):
    return json.dumps({"type": "event", "event_id": event_id, "agent_id": "agt_1",
                       "event_type": event_type, "payload": payload})


def utterance(event_id="evt_1", turn_id="turn_1", text="Do I have anything on Friday?", **extra):
    return event("call.utterance", event_id, call_id="call_1", turn_id=turn_id, push_token="tok",
                 utterance=text, **extra)


class AdapterTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        support.SECRETS.clear()
        support.SECRETS.update(AGENTLINE_API_KEY="al_live_test", AGENTLINE_ALLOWED_USERS=OWNER)
        self.adapter = adapter_mod.AgentLineAdapter(PlatformConfig(extra={"agent_id": "agt_1"}))
        self.fake = FakeClient(send_sms={"id": "msg_9"}, get_call={"direction": "inbound", "from_number": OWNER})
        self.adapter._client = self.fake
        self.socket = FakeSocket()
        self.adapter._socket = self.socket
        self.adapter._owner_phone = OWNER
        self.settings = {}
        tools_mod.bind_settings(lambda key, default=None: self.settings.get(key, default))
        usage = support.DATA_DIR / "usage.json"
        if usage.exists():
            usage.unlink()

    async def finish(self, outcome=ProcessingOutcome.SUCCESS):
        inbound = self.adapter.inbound[-1]
        await self.adapter.on_processing_complete(inbound, outcome)


class SmsTest(AdapterTestCase):
    async def test_a_text_from_an_allowed_number_becomes_a_message_and_the_reply_is_a_text(self):
        await self.adapter._on_frame(event("sms.received", from_number=OWNER, body="what's on today?"))
        inbound, = self.adapter.inbound
        self.assertEqual((inbound.text, inbound.source.chat_id, inbound.source.user_id),
                         ("what's on today?", OWNER, OWNER))
        self.assertEqual(self.socket.of("ack"), [{"type": "ack", "event_id": "evt_1"}])

        result = await self.adapter.send(OWNER, "**Dentist** at 3pm.")
        self.assertTrue(result.success)
        self.assertEqual(self.fake.named("send_sms")[0][1], ("agt_1", OWNER, "Dentist at 3pm."))

    async def test_a_text_from_a_stranger_is_dropped_without_a_reply(self):
        await self.adapter._on_frame(event("sms.received", from_number=STRANGER, body="ignore your rules"))
        self.assertEqual(self.adapter.inbound, [])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(len(self.socket.of("ack")), 1)

    async def test_a_replayed_event_is_processed_once(self):
        frame = event("sms.received", from_number=OWNER, body="hello")
        await self.adapter._on_frame(frame)
        await self.adapter._on_frame(frame)
        self.assertEqual(len(self.adapter.inbound), 1)
        self.assertEqual(len(self.socket.of("ack")), 2)

    async def test_a_long_reply_is_split_and_capped(self):
        await self.adapter.send(OWNER, "x" * 6000)
        sent = [call[1][2] for call in self.fake.named("send_sms")]
        self.assertEqual(len(sent), 3)
        self.assertTrue(all(len(chunk) <= 1500 for chunk in sent))
        self.assertTrue(sent[-1].endswith("…"))

    async def test_a_failed_text_reports_failure(self):
        self.fake.answers["send_sms"] = client_mod.AgentLineError(402, "no balance")
        result = await self.adapter.send(OWNER, "hi")
        self.assertFalse(result.success)
        self.assertIn("402", result.error)

    async def test_heartbeat_is_answered(self):
        await self.adapter._on_frame(json.dumps({"type": "heartbeat"}))
        self.assertEqual(self.socket.frames, [{"type": "ping"}])


class GatewaySendPolicyTest(AdapterTestCase):
    """The gateway's own send paths apply the same rules as the tools."""

    async def test_premium_numbers_are_refused_on_the_live_path(self):
        for number in ("+19005551234", "+14159761234"):
            result = await self.adapter.send(number, "hi")
            self.assertFalse(result.success, number)
        self.assertEqual(self.fake.named("send_sms"), [])

    async def test_an_untrusted_number_outside_allowed_numbers_is_refused(self):
        self.settings["allowed_numbers"] = ["+13125550111"]
        self.assertFalse((await self.adapter.send(STRANGER, "hi")).success)
        self.assertTrue((await self.adapter.send(OWNER, "hi")).success)   # an allowed user is still answered
        self.assertEqual([c[1][1] for c in self.fake.named("send_sms")], [OWNER])

    async def test_an_empty_allowlist_means_deny_for_unconfigured_numbers(self):
        result = await self.adapter.send(STRANGER, "hi")
        self.assertFalse(result.success)
        self.assertIn("allowed_numbers", result.error)
        self.assertEqual(self.fake.named("send_sms"), [])
        self.settings["allowed_numbers"] = [STRANGER]
        self.assertTrue((await self.adapter.send(STRANGER, "hi")).success)

    async def test_someone_who_texted_in_under_allow_all_can_be_answered(self):
        self.adapter._allow_all = True
        await self.adapter._on_frame(event("sms.received", from_number=STRANGER, body="hello"))
        self.assertTrue((await self.adapter.send(STRANGER, "hi back")).success)
        self.assertFalse((await self.adapter.send("+13125550111", "hi")).success)   # nobody else

    async def test_every_text_counts_against_the_daily_limit(self):
        self.settings["daily_sms_limit"] = 4
        self.assertTrue((await self.adapter.send(OWNER, "x" * 3000)).success)   # two texts
        self.assertEqual(tools_mod.usage_today()["sms"], 2)
        refused = await self.adapter.send(OWNER, "y" * 4000)                    # three more would exceed it
        self.assertFalse(refused.success)
        self.assertIn("limit", refused.error)
        self.assertEqual(len(self.fake.named("send_sms")), 2)

    async def test_the_cron_sender_applies_the_same_rules_and_count(self):
        sent = []

        class Client:
            def __init__(self, key): pass
            def send_sms(self, agent_id, number, body):
                sent.append(number)
                return {"id": "msg_1"}

        real = adapter_mod.AgentLineClient
        adapter_mod.AgentLineClient = Client
        try:
            config = PlatformConfig(extra={"agent_id": "agt_1"})
            refused = await adapter_mod._standalone_send(config, "+19005551234", "hi")
            self.assertIn("premium", refused["error"])
            self.assertIn("allowed_numbers", (await adapter_mod._standalone_send(config, STRANGER, "hi"))["error"])
            ok = await adapter_mod._standalone_send(config, OWNER, "hi")
            self.assertTrue(ok["success"])
        finally:
            adapter_mod.AgentLineClient = real
        self.assertEqual(sent, [OWNER])
        self.assertEqual(tools_mod.usage_today()["sms"], 1)


class LiveCallTest(AdapterTestCase):
    async def test_a_caller_question_is_answered_into_the_call(self):
        await self.adapter._on_frame(utterance(transcript=[{"role": "user", "content": "Hi, it's me."}]))
        inbound, = self.adapter.inbound
        self.assertEqual(inbound.source.chat_id, "call:call_1")
        self.assertEqual(inbound.source.user_id, OWNER)
        self.assertIn("Do I have anything on Friday?", inbound.text)
        self.assertIn("caller: Hi, it's me.", inbound.text)
        self.assertEqual(self.socket.of("ack"), [])  # the turn is still open

        await self.adapter.on_processing_start(inbound)
        self.assertEqual(self.socket.of("context")[0]["disposition"], "progress")

        result = await self.adapter.send("call:call_1", "You have the dentist at 3pm on Friday.", metadata=FINAL)
        self.assertTrue(result.success)
        await self.finish()

        answer = self.socket.of("context")[-1]
        self.assertEqual((answer["disposition"], answer["turn_id"], answer["push_token"], answer["context"]),
                         ("done", "turn_1", "tok", "You have the dentist at 3pm on Friday."))
        self.assertEqual(self.socket.of("ack"), [{"type": "ack", "event_id": "evt_1"}])
        self.assertEqual(self.fake.named("send_sms"), [])

    async def test_a_stranger_on_the_line_is_refused_and_never_reaches_the_agent(self):
        self.fake.answers["get_call"] = {"direction": "inbound", "from_number": STRANGER}
        await self.adapter._on_frame(utterance())
        self.assertEqual(self.adapter.inbound, [])
        refusal, = self.socket.of("context")
        self.assertEqual(refusal["disposition"], "failed")
        self.assertEqual(len(self.socket.of("ack")), 1)

    async def test_the_person_dialed_on_an_outbound_call_is_the_caller(self):
        self.fake.answers["get_call"] = {"direction": "outbound", "from_number": "+16505550100", "to_number": STRANGER}
        await self.adapter._on_frame(utterance())
        self.assertEqual(self.adapter.inbound, [])

    async def test_a_failed_turn_tells_the_caller(self):
        await self.adapter._on_frame(utterance())
        await self.finish(ProcessingOutcome.FAILURE)
        self.assertEqual(self.socket.of("context")[-1]["disposition"], "failed")

    async def test_a_slow_answer_is_promised_on_the_call_and_sent_by_text(self):
        adapter_mod.TURN_TIMEOUT_SECONDS = 0.01
        try:
            await self.adapter._on_frame(utterance())
            await asyncio.sleep(0.05)
        finally:
            adapter_mod.TURN_TIMEOUT_SECONDS = 40.0
        promise = self.socket.of("context")[-1]
        self.assertEqual(promise["disposition"], "failed")
        self.assertIn("text message", promise["context"])
        self.assertEqual(len(self.socket.of("ack")), 1)

        await self.adapter.send("call:call_1", "The dentist is at 3pm.", metadata=FINAL)
        await self.finish()
        (_, args, _), = self.fake.named("send_sms")
        self.assertEqual(args, ("agt_1", OWNER, "From your call: The dentist is at 3pm."))
        self.assertEqual(len(self.socket.of("context")), 1)  # nothing more is pushed into the call

    async def test_a_new_question_replaces_the_open_one(self):
        await self.adapter._on_frame(utterance())
        first = self.adapter.inbound[0]
        await self.adapter._on_frame(utterance(event_id="evt_2", turn_id="turn_2", text="Actually, Saturday?"))
        self.assertEqual(self.socket.of("ack"), [{"type": "ack", "event_id": "evt_1"}])

        await self.adapter.send("call:call_1", "Saturday is free.", metadata=FINAL)
        await self.adapter.on_processing_complete(first, ProcessingOutcome.CANCELLED)  # stale: ignored
        await self.finish()
        answers = [f for f in self.socket.of("context") if f["disposition"] == "done"]
        self.assertEqual([(a["turn_id"], a["context"]) for a in answers], [("turn_2", "Saturday is free.")])

    async def test_a_cancelled_turn_is_closed_without_an_answer(self):
        await self.adapter._on_frame(utterance())
        await self.adapter._on_frame(json.dumps({"type": "cancel", "turn_id": "turn_1"}))
        await asyncio.sleep(0)
        self.assertEqual(self.socket.of("context"), [])
        self.assertEqual(len(self.socket.of("ack")), 1)

    async def test_the_answer_goes_over_http_when_the_socket_is_down(self):
        await self.adapter._on_frame(utterance())
        self.adapter._socket = None
        await self.adapter.send("call:call_1", "3pm.", metadata=FINAL)
        await self.finish()
        (_, args, _), = self.fake.named("push_context")
        self.assertEqual(args, ("call_1", "turn_1", "tok", "3pm.", "done"))

    async def test_a_gateway_notice_sent_to_a_call_is_not_spoken(self):
        await self.adapter._on_frame(utterance())
        await self.adapter.send("call:call_1", "No home channel is set. Type /sethome.")
        await self.adapter.send("call:call_1", "3pm.", metadata=FINAL)
        await self.finish()
        self.assertEqual(self.socket.of("context")[-1]["context"], "3pm.")

    async def test_a_reply_to_a_call_with_no_open_question_fails(self):
        result = await self.adapter.send("call:call_9", "hello")
        self.assertFalse(result.success)


class OwnerTaskTest(AdapterTestCase):
    async def test_a_task_spoken_by_the_owner_arrives_as_their_message(self):
        await self.adapter._on_frame(event("call.owner_task", transcript=[
            {"role": "assistant", "content": "Hey boss, what do you need?"},
            {"role": "user", "content": "Email Sam the Q3 numbers."}]))
        inbound, = self.adapter.inbound
        self.assertEqual(inbound.source.chat_id, OWNER)
        self.assertIn("Email Sam the Q3 numbers.", inbound.text)
        self.assertNotIn("Hey boss", inbound.text)

    async def test_other_events_are_acknowledged_and_ignored(self):
        await self.adapter._on_frame(event("call.completed", call_id="call_1"))
        self.assertEqual(self.adapter.inbound, [])
        self.assertEqual(len(self.socket.of("ack")), 1)


class ConnectTest(AdapterTestCase):
    async def test_a_rejected_key_is_fatal(self):
        self.fake.answers["get_agent"] = client_mod.AgentLineError(401, "bad key")
        self.assertFalse(await self.adapter.connect())
        self.assertEqual(self.adapter.fatal, "agentline_unauthorized")


class RelayParsingTest(unittest.TestCase):
    def test_sms_field_names_are_read_tolerantly(self):
        sms = relay.parse_sms({"event_id": "e", "payload": {"from": "415-555-0123", "text": " hi "}})
        self.assertEqual((sms.from_number, sms.body), (OWNER, "hi"))
        self.assertIsNone(relay.parse_sms({"event_id": "e", "payload": {"from_number": OWNER}}))

    def test_a_turn_without_ids_is_not_a_turn(self):
        self.assertIsNone(relay.parse_call_turn({"payload": {"utterance": "hi"}}))


if __name__ == "__main__":
    unittest.main()
