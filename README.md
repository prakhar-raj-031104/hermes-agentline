# AgentLine for Hermes

Give Hermes a real phone number. With this plugin Hermes can:

- **Place phone calls.** Hermes writes the brief, a hosted voice holds the conversation, and
  Hermes gets the outcome and transcript back.
- **Send and read SMS.**
- **Be texted like any other chat.** A text from your phone becomes a Hermes conversation, and
  the reply comes back as a text.
- **Answer a live caller.** When you call your agent's number and ask something the voice cannot
  know ("do I have anything on Friday?"), the question reaches Hermes mid-call. Hermes answers
  with its own tools and memory, and the voice says the answer. If the answer takes longer than
  a call allows, it is texted to you instead.
- **Take tasks by voice.** A task you give on a call to your agent arrives as your message, and
  the result is texted back.

Every call, text and number purchase goes through Hermes's approval prompt first.

It uses [AgentLine](https://agentline.cloud) (US and Canadian numbers). The plugin connects
outward only: no public URL, tunnel or open port is needed.

## Install

```bash
hermes plugins install agentline
```

```bash
hermes plugins enable agentline
```

Installing asks for your AgentLine API key (`AGENTLINE_API_KEY`, from
[agentline.cloud](https://agentline.cloud)) and for consent to install one Python dependency,
`websockets`.

Then ask Hermes: *"Set up my phone line."* It creates a voice agent and, after you approve the
purchase, buys a number. A new AgentLine account gets its first number free for a 7-day trial.

## Text and call your agent

To use the phone line as a channel, add the numbers that may reach Hermes to the `.env` file in
your Hermes home (`hermes gateway setup` can do this for you), then run the gateway:

```
AGENTLINE_ALLOWED_USERS=+14155550123
```

```bash
hermes gateway run
```

| Variable | Meaning |
|---|---|
| `AGENTLINE_API_KEY` | Required. Your AgentLine API key. |
| `AGENTLINE_ALLOWED_USERS` | Phone numbers (E.164, comma-separated) allowed to text or call Hermes. |
| `AGENTLINE_AGENT_ID` | Which agent's number Hermes answers on. Not needed when the account has one agent. |
| `AGENTLINE_HOME_CHANNEL` | Number that receives cron and notification texts (`deliver=agentline`). |
| `AGENTLINE_ALLOW_ALL_USERS` | `true` lets any number drive the agent. For testing only. |

Set `AGENTLINE_HOME_CHANNEL` as well. Without it, Hermes sends its one-time "no home channel"
notice to each new number that texts, and that notice is a billed SMS.

For live call answers and voice tasks, also set the agent's owner number:
*"Set my owner phone to +14155550123."*

A number that is not on the allowlist is ignored: a text gets no reply, and a caller's question
is declined on the call without reaching Hermes.

## Tools

| Tool | What it does | Asks first |
|---|---|---|
| `agentline_status` | Balance, agents, numbers and today's usage | |
| `agentline_agent` | Create or update the voice agent | yes |
| `agentline_buy_number` | Buy a US or Canadian number | yes |
| `agentline_call` | Place a call and return the transcript | yes |
| `agentline_call_result` | Status and transcript of a call, or recent calls | |
| `agentline_hangup` | End a live call | |
| `agentline_sms` | Send a text | yes |
| `agentline_messages` | Read recent texts | |
| `agentline_contacts` | Find or save a contact | |

The plugin also bundles a skill, `agentline:agentline-phone`, that teaches the agent how to brief
a call and read the result.

## Settings

Under `plugins.entries.agentline.settings` in `config.yaml`, or the plugin's settings form in
Hermes Desktop:

| Key | Default | Meaning |
|---|---|---|
| `require_approval` | `true` | Send every call, text and number purchase to the approval prompt. |
| `allowed_numbers` | `[]` | Extra numbers that scheduled jobs and `send_message` may text. When set, also the only numbers the tools may call or text. |
| `default_agent_id` | `""` | Agent the tools act as. |
| `daily_call_limit` | `20` | Most calls the tools place per day. `0` removes the limit. |
| `daily_sms_limit` | `50` | Most texts sent per day, counting tool texts, replies and `send_message`. `0` removes the limit. |

## What you should know before installing

- **Network.** The plugin talks only to `api.agentline.cloud`: HTTPS requests from the tools, and
  one WebSocket (`wss://api.agentline.cloud/v1/events/ws`) held open while the gateway runs.
  What it sends: the call brief, text message bodies, destination numbers, contact names and
  numbers, and the agent's answers to live caller questions. AgentLine carries the call audio
  and produces the transcript; the plugin never handles audio.
- **Money.** Calls, texts and numbers are billed to your AgentLine account. A reply in a text
  conversation is one billed SMS per 1,500 characters, at most three per reply.
- **Approvals.** `agentline_call`, `agentline_sms`, `agentline_buy_number` and `agentline_agent`
  return an `approve` directive from a `pre_tool_call` hook, so Hermes's own approval gate decides.
  The prompt shows the whole call brief, opening line and voice, the whole text, or every agent
  field being changed; a brief over 2,000 characters is refused, not cut short. An approval is
  remembered for that exact content only. With nobody to approve (cron, an unattended gateway)
  the gate fails closed and nothing is dialed. Inside a scheduled run, `agentline_call` and
  `agentline_sms` are refused outright for numbers you did not configure.
- **Texts sent outside the agentline tools only reach numbers you configured.** Hermes's
  `send_message` (`agentline:+14155551234`), scheduled jobs (`cronjob_manage` with `deliver` or
  `failure_deliver` set to `agentline:...`) and gateway replies can run unattended, so they only
  text `AGENTLINE_ALLOWED_USERS`, the home channel and `allowed_numbers`. An empty `allowed_numbers`
  means no other number, not any number. A job that would text any other number is refused when
  the agent creates or updates it, and again when it fires. To schedule texts to someone new, add
  them to `allowed_numbers`. Replies to someone who texted or called in are the conversation
  itself and are allowed.
- **Always refused.** Emergency and service codes (911, 411, ...), premium-rate numbers and
  numbers outside the US and Canada are refused on every path, even when they are listed: in the
  hook, in the tools, and in the gateway's own send paths (replies, `send_message`, cron delivery).
- **Daily limits.** Every text counts against `daily_sms_limit`, whichever path sends it, and every
  call against `daily_call_limit`.
- **Untrusted input.** Transcripts and message bodies come from other people. Tool results label
  them as information, not instructions, and the skill tells the agent the same. SMS sender
  numbers can be spoofed by a determined attacker, so keep Hermes's approval prompts on for
  risky tools when you enable the text channel.
- **Stored data.** One file, `plugin-data/agentline/usage.json`, holding today's call and text
  counts. No transcripts, messages or credentials are stored by the plugin. The API key is read
  through Hermes's secret scope.
- **Background work.** The relay WebSocket runs inside the gateway process, only while the
  gateway runs. No shell commands, no child processes, no telemetry, no self-update.

## Limits

- US and Canadian numbers only.
- A live caller question must be answered within 40 seconds; after that the caller is told the
  answer will come by text.
- On a call that Hermes places to someone else, that person's questions are not routed to
  Hermes: they are not on your allowlist, and the voice works from the brief alone.

## Development

The tests need only the Python standard library:

```bash
python -m unittest discover -s tests -t .
```

`tests/integration_hermes.py` runs the plugin inside a real Hermes install against a local
stand-in for AgentLine (no account or network needed). Install and enable the plugin in a
scratch `HERMES_HOME`, then run it with Hermes's own interpreter:

```bash
HERMES_HOME=/path/to/scratch-home /path/to/hermes/venv/bin/python tests/integration_hermes.py
```

Validate against a Hermes checkout before pinning a commit for the catalog:

```bash
hermes plugins validate /path/to/hermes-agentline --install-deps
```

## License

MIT
