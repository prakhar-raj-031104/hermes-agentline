import json
import unittest

from tests import support
from tests.support import FakeClient, sequence

tools = support.submodule("tools")
guard = support.submodule("guard")
policy = support.submodule("policy")
client_mod = support.submodule("client")
plugin = support.load_plugin()

AGENT = {"id": "agt_1", "name": "Front desk", "owner_phone": None}


class RecordingContext:
    def __init__(self):
        self.tools, self.hooks, self.skills, self.platforms = {}, [], [], []

    def get_config(self, key, default=None):
        return default

    def register_tool(self, name, toolset, schema, handler, check_fn=None, **kwargs):
        self.tools[name] = handler

    def register_hook(self, name, callback, **kwargs):
        self.hooks.append(name)

    def register_skill(self, name, path):
        self.skills.append((name, path))

    def register_platform(self, **kwargs):
        self.platforms.append(kwargs)


class ToolTestCase(unittest.TestCase):
    def setUp(self):
        support.SECRETS.clear()
        support.SECRETS["AGENTLINE_API_KEY"] = "al_live_test"
        self.settings = {}
        tools.bind_settings(lambda key, default=None: self.settings.get(key, default))
        usage = support.DATA_DIR / "usage.json"
        if usage.exists():
            usage.unlink()
        self.fake = FakeClient(list_agents=[AGENT])
        self._real_client = tools._client
        tools._client = lambda: self.fake
        tools.POLL_SECONDS = 0

    def tearDown(self):
        tools._client = self._real_client

    def run_tool(self, tool, **args):
        return json.loads(tools._guarded(tools.HANDLERS[tool])(args, task_id="t"))


class RegistrationTest(unittest.TestCase):
    def test_register_declares_exactly_what_the_manifest_lists(self):
        ctx = RecordingContext()
        plugin.register(ctx)
        manifest = (support.PLUGIN_DIR / "plugin.yaml").read_text()
        declared = [line.strip()[2:] for line in manifest.split("provides_tools:")[1].split("provides_hooks:")[0]
                    .splitlines() if line.strip().startswith("- ")]
        self.assertEqual(sorted(ctx.tools), sorted(declared))
        self.assertEqual(ctx.hooks, ["pre_tool_call"])
        self.assertTrue(ctx.skills[0][1].is_file())
        platform = ctx.platforms[0]
        self.assertEqual(platform["name"], "agentline")
        self.assertTrue(platform["allowlist_suppresses_pairing"])
        self.assertNotIn("trusted_inbound", platform)


class CallToolTest(ToolTestCase):
    def test_call_waits_for_the_end_and_returns_the_transcript(self):
        self.fake.answers.update(
            create_call={"id": "call_1", "status": "initiated"},
            get_call=sequence({"id": "call_1", "status": "in-progress"}, {"id": "call_1", "status": "completed"}),
            get_transcript=[{"role": "human", "text": "Friday at 7:30 is fine."}])
        result = self.run_tool("agentline_call", to_number="(415) 555-0123", objective="Book a table for 4.")
        self.assertTrue(result["ok"])
        self.assertEqual(result["call"]["status"], "completed")
        self.assertEqual(result["call"]["transcript"][0]["text"], "Friday at 7:30 is fine.")
        (_, args, kwargs), = self.fake.named("create_call")
        self.assertEqual(args, ("agt_1", "+14155550123"))
        self.assertEqual(kwargs["system_prompt"], "Book a table for 4.")

    def test_a_transcript_delivered_as_a_json_string_is_returned_as_turns(self):
        raw = json.dumps([{"role": "agent", "text": "Hello?", "timestamp": "2026-11-10T22:02:59Z"}])
        self.fake.answers.update(create_call={"id": "call_1", "status": "completed"},
                                 get_transcript={"call_id": "call_1", "status": "completed", "transcript": raw})
        result = self.run_tool("agentline_call", to_number="+14155550123", objective="Say hi.")
        self.assertEqual(result["call"]["transcript"], [{"role": "agent", "text": "Hello?"}])

    def test_call_without_wait_returns_at_once(self):
        self.fake.answers.update(create_call={"id": "call_1", "status": "initiated"})
        result = self.run_tool("agentline_call", to_number="+14155550123", objective="Say hi.", wait=False)
        self.assertTrue(result["ok"])
        self.assertEqual(self.fake.named("get_call"), [])

    def test_emergency_and_premium_numbers_never_dial(self):
        for number in ("911", "+1911", "+19005551234", "+14159761234", "+442071234567", "nonsense"):
            result = self.run_tool("agentline_call", to_number=number, objective="x")
            self.assertFalse(result["ok"], number)
        self.assertEqual(self.fake.named("create_call"), [])

    def test_daily_call_limit_stops_further_calls(self):
        self.settings["daily_call_limit"] = 1
        self.fake.answers.update(create_call={"id": "call_1", "status": "completed"}, get_transcript=[])
        self.assertTrue(self.run_tool("agentline_call", to_number="+14155550123", objective="x")["ok"])
        second = self.run_tool("agentline_call", to_number="+14155550123", objective="x")
        self.assertFalse(second["ok"])
        self.assertIn("limit", second["error"])
        self.assertEqual(len(self.fake.named("create_call")), 1)

    def test_api_failure_is_an_error_result_not_an_exception(self):
        self.fake.answers.update(create_call=client_mod.AgentLineError(402, "insufficient balance"))
        result = self.run_tool("agentline_call", to_number="+14155550123", objective="x")
        self.assertFalse(result["ok"])
        self.assertIn("402", result["error"])
        self.assertIn("agentline_status", result["hint"])
        self.assertEqual(tools.usage_today()["calls"], 0)

    def test_missing_api_key_is_reported(self):
        tools._client = self._real_client
        support.SECRETS.clear()
        result = self.run_tool("agentline_status")
        self.assertFalse(result["ok"])
        self.assertIn("AGENTLINE_API_KEY", result["error"])
        self.assertFalse(tools.has_api_key())


class SmsAndContactsTest(ToolTestCase):
    def test_sms_sends_from_the_only_agent(self):
        self.fake.answers.update(send_sms={"id": "msg_1", "status": "sent"})
        result = self.run_tool("agentline_sms", to_number="+14155550123", body="Table confirmed for 7pm.")
        self.assertTrue(result["ok"])
        self.assertEqual(self.fake.named("send_sms")[0][1], ("agt_1", "+14155550123", "Table confirmed for 7pm."))
        self.assertEqual(tools.usage_today()["sms"], 1)

    def test_sms_outside_the_allowlist_is_refused(self):
        self.settings["allowed_numbers"] = ["+14155550123"]
        result = self.run_tool("agentline_sms", to_number="+12125550199", body="hi")
        self.assertFalse(result["ok"])
        self.assertEqual(self.fake.named("send_sms"), [])

    def test_saving_a_known_number_updates_the_contact(self):
        self.fake.answers.update(list_contacts={"contacts": [{"id": "con_1"}]}, update_contact={"id": "con_1"})
        result = self.run_tool("agentline_contacts", action="save", name="Dr. Patel",
                               phone_number="+14155550123", label="dentist")
        self.assertTrue(result["updated"])
        self.assertEqual(self.fake.named("create_contact"), [])

    def test_several_agents_need_an_explicit_choice(self):
        self.fake.answers.update(list_agents=[AGENT, {"id": "agt_2", "name": "Sales"}])
        result = self.run_tool("agentline_sms", to_number="+14155550123", body="hi")
        self.assertFalse(result["ok"])
        self.assertIn("agt_2", result["error"])


class ApprovalHookTest(ToolTestCase):
    def test_a_call_asks_for_approval_scoped_to_the_number(self):
        directive = guard.pre_tool_call("agentline_call", {"to_number": "415-555-0123", "objective": "Book a table"},
                                        task_id="t")
        self.assertEqual(directive["action"], "approve")
        self.assertEqual(directive["rule_key"], "agentline:agentline_call:+14155550123")
        self.assertIn("Book a table", directive["message"])

    def test_a_blocked_destination_is_refused_even_with_approval_off(self):
        self.settings["require_approval"] = False
        directive = guard.pre_tool_call("agentline_sms", {"to_number": "911", "body": "x"})
        self.assertEqual(directive["action"], "block")

    def test_approval_can_be_turned_off_for_allowed_destinations(self):
        self.settings["require_approval"] = False
        self.assertIsNone(guard.pre_tool_call("agentline_call", {"to_number": "+14155550123", "objective": "x"}))

    def test_buying_a_number_asks_and_other_tools_pass(self):
        self.assertEqual(guard.pre_tool_call("agentline_buy_number", {})["action"], "approve")
        self.assertIsNone(guard.pre_tool_call("agentline_status", {}))
        self.assertIsNone(guard.pre_tool_call("terminal", {"command": "ls"}))


class PolicyTest(unittest.TestCase):
    def test_numbers_normalize_to_e164(self):
        self.assertEqual(policy.normalize_number("(415) 555-0123"), "+14155550123")
        self.assertEqual(policy.normalize_number("1 415 555 0123"), "+14155550123")
        self.assertEqual(policy.normalize_number("+44 20 7123 4567"), "")
        self.assertEqual(policy.number_list("+14155550123, junk ,4155550124"), ["+14155550123", "+14155550124"])


if __name__ == "__main__":
    unittest.main()
