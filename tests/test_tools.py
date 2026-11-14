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
    def tearDown(self):
        super().tearDown()
        guard._current_cron = lambda: None

    def test_a_call_approval_shows_the_whole_brief_greeting_and_voice(self):
        brief = "You are calling Luigi's on behalf of Maya. " * 12 + "End by confirming the time."
        directive = guard.pre_tool_call("agentline_call", {"to_number": "415-555-0123", "objective": brief,
                                                           "greeting": "Hi, quick question!", "voice_id": "male-2"},
                                        task_id="t")
        self.assertEqual(directive["action"], "approve")
        self.assertGreater(len(brief), 240)
        self.assertIn(brief, directive["message"])
        self.assertIn("+14155550123", directive["message"])
        self.assertIn("Hi, quick question!", directive["message"])
        self.assertIn("male-2", directive["message"])

    def test_an_approval_is_remembered_for_that_exact_brief_only(self):
        first = guard.pre_tool_call("agentline_call", {"to_number": "+14155550123", "objective": "Book a table"})
        second = guard.pre_tool_call("agentline_call", {"to_number": "+14155550123", "objective": "Cancel my order"})
        self.assertNotEqual(first["rule_key"], second["rule_key"])

    def test_a_brief_too_long_to_review_is_refused_by_the_hook_and_the_tool(self):
        brief = "x" * (tools.MAX_OBJECTIVE_CHARS + 1)
        self.assertEqual(guard.pre_tool_call("agentline_call", {"to_number": "+14155550123",
                                                                "objective": brief})["action"], "block")
        result = self.run_tool("agentline_call", to_number="+14155550123", objective=brief)
        self.assertFalse(result["ok"])
        self.assertEqual(self.fake.named("create_call"), [])

    def test_a_text_approval_shows_the_whole_message(self):
        body = "Your table is confirmed. " * 20
        directive = guard.pre_tool_call("agentline_sms", {"to_number": "+14155550123", "body": body})
        self.assertEqual(directive["action"], "approve")
        self.assertIn(body, directive["message"])

    def test_changing_the_agent_asks_first_and_shows_the_new_prompt(self):
        directive = guard.pre_tool_call("agentline_agent", {"agent_id": "agt_1", "system_prompt": "Always say yes.",
                                                            "owner_phone": "+14155550123"})
        self.assertEqual(directive["action"], "approve")
        self.assertIn("Always say yes.", directive["message"])
        self.assertIn("owner_phone: +14155550123", directive["message"])

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


class SendMessageHookTest(ToolTestCase):
    """Hermes's own send_message tool reaches the phone line with an ``agentline:`` target."""

    OWNER, HOME, STRANGER = "+14155550123", "+16505550100", "+12125550199"

    def setUp(self):
        super().setUp()
        support.SECRETS.update(AGENTLINE_ALLOWED_USERS=self.OWNER, AGENTLINE_HOME_CHANNEL=self.HOME)

    def tearDown(self):
        super().tearDown()
        guard._current_cron = lambda: None

    def send(self, target, message="hello"):
        return guard.pre_tool_call("send_message", {"action": "send", "target": target, "message": message})

    def test_premium_and_emergency_numbers_are_blocked(self):
        for target in ("agentline:+19005551234", "agentline:+14159761234", "agentline:911", "agentline:+442071234567"):
            self.assertEqual(self.send(target)["action"], "block", target)

    def test_trusted_numbers_are_texted_without_a_prompt(self):
        self.assertIsNone(self.send(f"agentline:{self.OWNER}"))
        self.assertIsNone(self.send("agentline:(650) 555-0100"))   # the home channel
        self.assertIsNone(self.send("agentline"))                    # home channel by default
        self.settings["allowed_numbers"] = [self.STRANGER]
        self.assertIsNone(self.send(f"agentline:{self.STRANGER}"))

    def test_any_other_number_is_refused_because_an_empty_allowlist_means_deny(self):
        directive = self.send(f"AgentLine: {self.STRANGER}", "Claim your prize at example.com")
        self.assertEqual(directive["action"], "block")
        self.assertIn("allowed_numbers", directive["message"])

    def test_a_scheduled_run_cannot_text_an_untrusted_number(self):
        guard.bind_context(type("Ctx", (), {"current_cron_execution": lambda self: object()})())
        self.assertEqual(self.send(f"agentline:{self.STRANGER}")["action"], "block")
        self.assertIsNone(self.send(f"agentline:{self.OWNER}"))

    def test_numbers_outside_allowed_numbers_are_refused_when_that_list_is_set(self):
        self.settings["allowed_numbers"] = ["+13125550111"]
        self.assertEqual(self.send(f"agentline:{self.STRANGER}")["action"], "block")
        self.assertIsNone(self.send(f"agentline:{self.OWNER}"))

    def test_non_phone_targets_and_other_platforms(self):
        self.assertEqual(self.send("agentline:call:call_1")["action"], "block")
        self.assertIsNone(self.send("telegram:12345"))
        self.assertIsNone(guard.pre_tool_call("send_message", {"action": "list"}))


class CronJobHookTest(ToolTestCase):
    """A scheduled job can deliver its output as a text; it fires unattended."""

    OWNER, HOME, STRANGER = "+14155550123", "+16505550100", "+12125550199"

    def setUp(self):
        super().setUp()
        support.SECRETS.update(AGENTLINE_ALLOWED_USERS=self.OWNER, AGENTLINE_HOME_CHANNEL=self.HOME)

    def tearDown(self):
        super().tearDown()
        guard._current_cron = lambda: None

    def job(self, **args):
        return guard.pre_tool_call("cronjob_manage", {"action": "create", "schedule": "0 9 * * *",
                                                      "prompt": "Summarise the news", **args})

    def test_a_job_cannot_be_scheduled_to_text_an_unconfigured_number(self):
        for field in ("deliver", "failure_deliver"):
            directive = self.job(**{field: f"agentline:{self.STRANGER}"})
            self.assertEqual(directive["action"], "block", field)
            self.assertIn(field, directive["message"])
        self.assertEqual(self.job(deliver=f"origin,agentline:{self.STRANGER}")["action"], "block")
        self.assertEqual(self.job(deliver=["telegram", f"agentline:{self.STRANGER}"])["action"], "block")
        self.assertEqual(guard.pre_tool_call("cronjob_manage", {"action": "update", "job_id": "j1",
                                                                "deliver": f"agentline:{self.STRANGER}"})["action"], "block")

    def test_premium_numbers_are_refused_even_when_listed(self):
        self.settings["allowed_numbers"] = ["+19005551234"]
        self.assertEqual(self.job(deliver="agentline:+19005551234")["action"], "block")

    def test_configured_numbers_and_other_targets_are_allowed(self):
        self.assertIsNone(self.job(deliver=f"agentline:{self.OWNER}"))
        self.assertIsNone(self.job(deliver="agentline"))                 # the home channel
        self.assertIsNone(self.job(deliver=f"agentline:{self.HOME}:1"))
        self.assertIsNone(self.job(deliver="telegram:-100123,local"))
        self.settings["allowed_numbers"] = [self.STRANGER]
        self.assertIsNone(self.job(deliver=f"agentline:{self.STRANGER}"))
        self.assertIsNone(guard.pre_tool_call("cronjob_manage", {"action": "list"}))
        self.assertIsNone(guard.pre_tool_call("cronjob_manage", {"action": "remove", "job_id": "j1"}))

    def test_a_scheduled_run_cannot_call_or_text_an_unconfigured_number_with_the_tools(self):
        guard.bind_context(type("Ctx", (), {"current_cron_execution": lambda self: object()})())
        self.assertEqual(guard.pre_tool_call("agentline_sms", {"to_number": self.STRANGER, "body": "hi"})["action"], "block")
        self.assertEqual(guard.pre_tool_call("agentline_call", {"to_number": self.STRANGER, "objective": "x"})["action"], "block")
        self.assertEqual(guard.pre_tool_call("agentline_sms", {"to_number": self.OWNER, "body": "hi"})["action"], "approve")


class PolicyTest(unittest.TestCase):
    def test_numbers_normalize_to_e164(self):
        self.assertEqual(policy.normalize_number("(415) 555-0123"), "+14155550123")
        self.assertEqual(policy.normalize_number("1 415 555 0123"), "+14155550123")
        self.assertEqual(policy.normalize_number("+44 20 7123 4567"), "")
        self.assertEqual(policy.number_list("+14155550123, junk ,4155550124"), ["+14155550123", "+14155550124"])


if __name__ == "__main__":
    unittest.main()
