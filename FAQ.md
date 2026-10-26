# FAQ

## Getting started

### Do I need a VPN?

**For phone access, yes — use a VPN or mesh network.** Tailscale is
the easiest.

This isn't a stylistic preference. iOS Safari refuses microphone
access on a page served over plain HTTP, and it won't reliably open
the mic on a self-signed certificate you haven't explicitly trusted.
On a LAN IP you'll tap the orb and nothing will happen.

The app itself has **no authentication**. It binds to `0.0.0.0` and
listens on one port. That port must live inside a trusted network.

Never port-forward it to the open internet.

### What's the difference between Tailscale and a plain LAN?

Tailscale gives you two things:

1. **A routable private address** for the server, on every device.
2. **Real, trusted HTTPS certificates** via `tailscale cert`, so the
   microphone works without a warning prompt.

On a plain LAN you get the first only, if your router gives the server
a stable address. The certificate warning stays, and on iOS the
microphone may refuse to open.

### What's the minimum setup to try it?

1. `pip install -r requirements.txt`
2. `whisper.cpp` running on `127.0.0.1:8178`
3. `piper` installed with one voice model
4. `ffmpeg` on `PATH`
5. `BRAIN_API_KEY` in `.env`
6. Tailscale on both devices

You do **not** need Kokoro, the Telegram bridge, or the memory layer.

### Do I need an Apple Silicon Mac?

No. The code is portable and runs on Linux. Apple Silicon is just
recommended because `whisper.cpp` uses Metal and it's much faster.

Any machine with a CPU will work — just expect the transcription lag.

### What does it cost to run?

Nothing but electricity, plus whatever your LLM provider charges per
token. Whisper and Piper are free and local.

---

## Setup and configuration

### Which environment variables are actually required?

Only `BRAIN_API_KEY`. Everything else has a working default.

The ones worth setting explicitly:

| Variable | Why |
|---|---|
| `AVA_BARGE_IN=1` | **Required for duplex.** Off by default. |
| `AVA_TLS_CERT` / `AVA_TLS_KEY` | Needed for phone access. |
| `PIPER_MODEL` | If your voice model isn't the default name. |
| `WHISPER_URL` | If whisper.cpp isn't on the default port. |

### How do I use a different LLM provider?

The brain speaks the Anthropic Messages API. Point it at any
compatible endpoint:

```bash
BRAIN_API_URL=https://api.anthropic.com/v1/messages
BRAIN_MODEL=claude-sonnet-4-5
BRAIN_API_KEY=sk-ant-...
```

Other providers work if they expose the same shape. Check `brain.py`
if a provider's streaming format differs.

### How do I change the voice?

Download the voice model you want, then:

```bash
PIPER_MODEL_DIR=~/.local/share/piper/voices
PIPER_MODEL=en_US-amy-medium
```

Both files are needed: `en_US-amy-medium.onnx` and
`en_US-amy-medium.onnx.json`.

### Can I use Kokoro instead of Piper?

Yes. Set `AVA_TTS_PROVIDER=kokoro`, `KOKORO_URL`, and `KOKORO_VOICE`.

Kokoro sounds better but renders more slowly — noticeable on long
replies.

### How do I regenerate the TLS certificate?

```bash
openssl req -x509 -newkey rsa:4096 -nodes \
  -keyout certs/server.key -out certs/server.crt \
  -days 365 -subj "/CN=ai-agent-chat" \
  -addext "subjectAltName=DNS:ai-agent-chat,DNS:localhost,IP:127.0.0.1"
```

For a tailnet, `tailscale cert <hostname>` is better — it's a real
trusted certificate, so there's no warning.

### Why isn't my new environment variable taking effect?

If the bridge runs under launchd, `launchctl kickstart -k` reuses the
**cached** job definition. A new variable in the plist won't reach
the process.

Two options:

1. Put the variable in `.env` as well. The app loads `.env`.
2. Reload the plist:
   ```bash
   launchctl bootout gui/$(id -u)/com.agent-voice-ai.bridge
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.agent-voice-ai.bridge
   ```

Then confirm with `GET /api/health`. `ps eww` doesn't show it
reliably.

---

## Using it

### What's the difference between Push to talk and Duplex?

**Push to talk:** tap the orb, talk, release. The mic opens while you
hold it and closes when you let go.

**Duplex:** tap once, and the mic stays open. You talk whenever you
want. If you talk over the agent, it stops mid-sentence and answers
what you just said.

### Do I have to tap the orb first in duplex mode?

Yes, once per page load. iOS requires a user gesture before any page
can open the microphone. After the first tap the mic stays open for
the whole session.

### Why does it interrupt me when I'm not talking?

Two possibilities:

- **Playback bleed.** The agent's own voice is loud enough to trip the
  detector. The thresholds are tuned so this doesn't happen, but a
  very quiet room with a loud voice can cause it. Try moving the
  phone away from the speaker.
- **Background noise.** A fan, a car, a TV. The quiet floor is
  calibrated at session start; if noise starts after that, it may
  exceed the threshold.

If it happens constantly, turn on `AVA_BARGE_TRACE=1` and look at the
RMS values in the log.

### Can I use it with the screen off?

The page has to stay open and foregrounded on iOS — the browser
suspends the tab in the background and the mic stops.

There's no native app and no background-mode support yet.

### What does the mute button do?

It disables the microphone **track** entirely — the OS stops
capturing audio, not just the upload. Useful when you want the agent
to keep talking but not listen.

### What happens if I talk while it's thinking?

It interrupts. That's the point of duplex mode. The agent abandons
the in-progress answer and responds to what you just said.

### Why did it answer twice?

Two possibilities:

- **Stale cached page.** The double-play bug was fixed in v0.10.1. A
  cached page still has the old client code. Hard-refresh, or clear
  the site data.
- **A fallback plus a stream.** The server sends a complete WAV as a
  Safari fallback. The current client only plays it when no streamed
  audio arrived. If you're hearing both, you're on an old page.

### Why did the reply restart partway through?

That was the WebSocket ping timeout closing the connection mid-answer.
Fixed in v0.10.2 by disabling the ping timeout and sending keepalives
between sentences.

### Why is the first reply so slow?

Cold start. The model warms up, and the agent may run tool calls
before it speaks. Subsequent turns are much faster.

### Can I use the text box instead of talking?

Yes. The input field sends a text turn through the same agent loop —
it just skips STT. Useful when you're in a quiet place or the mic
isn't cooperating.

### Does it work on Android?

The frontend is standard browser APIs. It should work, but iOS Safari
is what it's been tested against. The one Android-specific thing to
check is that your browser doesn't suspend the page's microphone when
it's backgrounded.

---

## Troubleshooting

### The mic button does nothing

The page isn't on trusted HTTPS, or the certificate isn't trusted.
Check:

1. Is the address bar showing a padlock or a warning?
2. Does `GET /api/health` work from the same browser?
3. Is `AVA_BARGE_IN=1` set?

On iOS, a self-signed cert that you've only dismissed once is not
the same as one you've explicitly trusted. Installing it in the
profile settings is what makes the mic prompt appear.

### "I can hear you clearly" but it never answers

Check `/api/health`:

```bash
curl -sk https://localhost:8770/api/health | python3 -m json.tool
```

If `barge_in.enabled` is `false`, duplex is off — set `AVA_BARGE_IN=1`
and restart.

### The transcript is empty / it didn't hear me

1. Is the whisper.cpp server up? `curl $WHISPER_URL` should respond.
2. Is the audio getting through? Turn on `AVA_BARGE_TRACE=1` and look
   for RMS values that aren't zero.
3. Is ffmpeg installed? The browser sends webm; whisper needs WAV.

### The bridge won't start

- **Port already in use.** `lsof -i :8770`. The supervised instance
  is probably running — don't start a second copy.
- **Import error.** Activate the venv first. The runtime venv here is
  `.venv-iris`, not `.venv` — check which one your plist points at.
- **Missing certs.** The bridge auto-detects `certs/`. If they exist
  but are unreadable it'll fail on TLS — check the paths.

### It works on my laptop but not on my phone

Almost always one of:

1. The phone isn't on the VPN.
2. The cert isn't trusted on the phone.
3. The phone is using a different browser than the one you tested
   with.

### How do I see the logs?

If it's under launchd:

```bash
tail -f /path/to/your/logs/bridge.err
```

The real log is `bridge.err` — Python's logging writes to stderr.
`bridge.log` is mostly empty.

### How do I tell if the agent is actually transcribing correctly?

`GET /api/health` reports the whisper and memory layer status. For
a live check, use the text box: if a text turn answers instantly but a
voice turn doesn't, the problem is STT.

---

## Security and privacy

### Does my audio leave the machine?

No. Whisper and Piper run locally. The audio goes from the browser to
the bridge on your machine, and the transcript goes to the LLM
provider — that's the one thing that leaves.

### What does get sent to the LLM?

The transcript, the conversation so far, your system prompt, and
whatever memory context is configured. If you have the memory layer
enabled, files you point it at are included in the prompt.

### Is the bridge authenticated?

No. See [SECURITY.md](SECURITY.md). Keep it inside a VPN.

### Can it send things on my behalf?

Yes, if you enable the Telegram tool — and irreversible external
actions are held for explicit confirmation unless you use a
pre-approval phrase like "just send it."

### Is it safe to run on a public server?

Not as-is. Put an authenticating reverse proxy in front of it, and
keep the bridge itself bound to a private interface.

---

## Development

### How do I run the tests?

```bash
cd ai_agent_chat
source .venv/bin/activate

python tests/test_barge_detector_unit.py   # no server needed
python tests/test_duplex_real.py           # needs the bridge running
```

Most tests need the bridge running with `AVA_BARGE_IN=1`.

### The speech tests skip instead of failing

They couldn't find Piper or a voice model. Set:

```bash
export AVA_TEST_PIPER=/path/to/piper
export AVA_TEST_PIPER_MODEL=/path/to/voice.onnx
```

### How do I add a tool?

1. Add the function to `tools.py`.
2. Register it in the tool schema in `brain.py`.
3. Decide its tier: read-only (auto-invoke), reversible (auto-invoke
   and report), or irreversible external (hold for confirmation).

See [AGENTS.md](AGENTS.md) for the details, and ask before adding
anything broad — the voice surface is deliberately narrow.

### How do I add a wake word?

Not supported yet. The design notes are in [AGENTS.md](AGENTS.md);
it's a planned feature, not a config toggle.

### Why is there a `barge.py` copy of another project's detector?

To keep this repo standalone. Importing from another agent framework
would mean requiring it to be installed. The port is deliberate —
see [AGENTS.md](AGENTS.md) for the invariants it must preserve.

### How do I bump the version?

Two places: `FastAPI(... version="X.Y.Z")` and the `vX.Y.Z` string in
the embedded HTML. Change both, restart, then verify:

```bash
curl -sk https://localhost:8770/ | grep vX.Y.Z
```
