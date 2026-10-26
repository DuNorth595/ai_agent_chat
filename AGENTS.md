# AGENTS.md — working on this codebase

> If you're an LLM agent picking this up to help maintain or extend it,
> read this file before you touch anything. It distills the
> load-bearing invariants and the failure modes that have already cost
> hours on this project.

## What this project is

A self-hosted, mobile-first voice agent bridge. The operator runs
`whisper.cpp` (STT) and `piper` / `kokoro` (TTS) locally; the LLM call
goes to any Anthropic-compatible API. The bridge serves a single-page
HTML/JS frontend over HTTPS that a phone browser can reach over a
VPN/mesh network.

The headline feature is **real full-duplex conversation**: in duplex
mode the microphone stays live, and talking over the agent interrupts
it mid-sentence. The words used to interrupt become the next request.

Current version: **v0.10.7**.

## Repo map

```
agent_voice_ai.py   — The main module. FastAPI app, the embedded HTML/JS
                      page, the /ws WebSocket handler, the duplex loop,
                      barge-in wiring, TTS shipping, audio fallback.
barge.py            — Voice-activity detector (the barge-in engine).
                      Portable; no imports outside the stdlib + numpy.
brain.py            — LLM streaming wrapper, prompt caching, tool calls.
tools.py            — The agent's tool surface.
memory.py           — Long-term memory context (optional, off by default).
whisper_stt.py      — whisper.cpp client (HTTP POST, WAV body).
piper_tts.py        — Piper CLI wrapper.
kokoro_tts.py       — Kokoro fallback TTS.
skills_bridge.py    — Optional skills context injection.
telegram_sync.py    — Optional Telegram bridge (poller).

tests/              — Real end-to-end tests. See "Testing" below.
docs/screenshots/   — Screenshots used in the README.
examples/           — macOS launchd plist template.
```

`agent_voice_ai.py` is large (~3,900 lines) because the frontend is an
embedded HTML string. That is intentional — there is no build step and
no asset pipeline.

## Load-bearing invariants

**These are the things that will silently break if you change them.**

1. **One reader task owns the WebSocket for the whole duplex session.**
   There is exactly one `_barge_reader` per session. It reads PCM
   frames continuously, for the whole session — not per turn. The
   collect phase and the reply phase share that one reader and that one
   detector.

   *Why:* earlier versions had the reader exit on `barge_stop` (thinking
   that meant "turn over"). It doesn't — it means "the user stopped
   talking." The reader then wasn't running during the agent's reply,
   so the agent never heard the user interrupt it. It would capture the
   interruption *after* finishing its reply, which is the exact bug
   users described as "it captures me but keeps talking."

   *How to check:* `grep -n "_barge_reader" agent_voice_ai.py`. If you
   find more than one creation site, you've broken it.

2. **`barge_stop` does not end the duplex session.** It ends the
   current utterance. The next utterance starts on the next frames.

3. **The quiet floor is calibrated before playback starts.** The
   detector learns the room's noise floor from the first frames. If
   you calibrate during playback, the agent's own echo becomes "quiet"
   and the detector never trips.

4. **Playback bleed must not trip the detector.** There is a unit test
   for exactly this (`test_barge_detector_unit.py`). Don't loosen the
   thresholds to "fix" something else.

5. **`barge.py` must not import from outside this repo.** It is a
   deliberate port of a detector from another agent framework, adapted
   for audio over a WebSocket. Porting keeps this repo standalone —
   no dependency on another project being installed. Don't "clean it
   up" into an import.

6. **The client must not play both streamed and fallback audio.** The
   server sends a complete `/api/audio/<id>.wav` as a Safari fallback.
   The client plays it **only** if zero WebSocket audio frames arrived
   for that turn. Unconditionally playing both is the "it answers
   twice" bug. There's a regression test.

7. **Uvicorn's `ws_ping_timeout` is disabled, and `keepalive` frames
   are sent between sentences.** A long reply exceeds the default
   20-second ping timeout and the socket drops mid-reply. Don't
   re-enable the default.

8. **The WebSocket handler must not block on the brain.** Brain
   generation is abortable. If the handler awaits the whole reply
   before reading the next frame, barge-in cannot work at all.

## The barge-in state machine

```
                    ┌─────────────┐
                    │   IDLE      │
                    └──────┬──────┘
                           │ speech detected
                    ┌──────▼──────┐
                    │  COLLECTING │◀──┐
                    └──────┬──────┘   │ more speech
                           │ silence   │
                    ┌──────▼──────┐   │
                    │  ENDPOINTING│───┘
                    └──────┬──────┘
                           │ utterance complete
                    ┌──────▼──────┐
                    │ TRANSCRIBING│
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  THINKING   │◀── barge_in (generation)
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  SPEAKING   │◀── barge_in (playback) → abort
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │    IDLE     │  ← the session continues; the mic
                    └─────────────┘     is still live
```

Two things make this work that aren't obvious:

- The **reply phase and the collect phase are the same task.** The
  agent's reply is not a blocking call in the reader.
- **`playing` is signalled by the client**, not inferred server-side.
  Audio is queued in the browser and starts *after* the server has
  finished sending. The server can't know when playback begins, so the
  client tells it.

## Detector constants

```python
SILENCE_RMS_THRESHOLD     = 200     # what counts as "quiet"
PLAYBACK_MIN_TRIGGER      = 1500.0  # minimum RMS to trip while speaking
TRIGGER_CEILING           = 4000.0  # cap so a loud room can't over-trigger
DEFAULT_BARGE_MULTIPLIER  = 3.0     # × the quiet floor
```

Tune with `AVA_BARGE_TRACE=1` and read the RMS trace in the log. Turn
it off in production — it's noisy.

## Testing

Tests are **real end-to-end tests**, not mocks. They drive a running
bridge over a real WebSocket and assert on real behaviour.

```bash
cd /path/to/ai_agent_chat
source .venv/bin/activate

# Needs no server:
python tests/test_barge_detector_unit.py

# Needs the bridge running with AVA_BARGE_IN=1:
python tests/test_duplex_real.py
python tests/test_duplex_interrupt.py
python tests/test_duplex_twice.py
python tests/test_mute.py
python tests/test_barge_ab.py
python tests/test_barge_playback.py
python tests/test_no_double_play.py
```

`tests/_testcfg.py` resolves the Piper binary and voice model. It
checks, in order:

1. `AVA_TEST_PIPER` / `AVA_TEST_PIPER_MODEL` environment variables
2. A repo-local `.venv` or `.venv-iris`
3. `piper` on `PATH`
4. Common install locations

If it can't find them the speech tests **skip** rather than fail. That
is deliberate — a contributor without Piper shouldn't get a red build.

**Never hardcode an absolute path to a model or binary in a test.**
Use `_testcfg.py`.

## Making changes safely

1. **Edit the source.**
2. **Restart the bridge.** If it's under launchd:
   ```bash
   launchctl kickstart -k gui/$(id -u)/com.agent-voice-ai.bridge
   ```
3. **Verify what is actually being served.** The patch tool reporting
   success does not mean the running process picked it up:
   ```bash
   curl -sk https://localhost:8770/ | grep -i "your-feature"
   ```
4. **Run the tests.**

### The launchd env-var trap

`launchctl kickstart -k` reuses the **cached** job definition. A new
environment variable added to the plist will not reach the running
process until the plist is reloaded:

```bash
launchctl bootout gui/$(id -u)/com.agent-voice-ai.bridge
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.agent-voice-ai.bridge
```

The app also loads `.env`, so putting the variable in **both** places
is the safe move. Confirm what the process actually has with:

```bash
curl -sk https://localhost:8770/api/health | python3 -m json.tool
```

`ps eww` is not reliable for this.

## Failure modes that have already happened — do not reintroduce

| Symptom | Root cause | Fix in place |
|---|---|---|
| "It captures my interruption but keeps talking" | The duplex reader exited on `barge_stop` | One reader for the whole session |
| "It answers twice" | Fallback WAV played *and* streamed audio played | Count WS audio frames per turn; fallback only if zero |
| "The reply restarts partway through" | Same as above | Same fix |
| "The reply gets cut off after ~50 s" | Uvicorn's default `ws_ping_timeout=20` | `ws_ping_timeout=None` + `keepalive` frames |
| "The mic doesn't release after a reply" | Client kept the capture track alive across turns | Single duplex session; one continuous capture |
| "It never interrupts" | The handler awaited the brain before reading the next frame | Abortable brain stream + background reader |
| "whisper.cpp returns HTTP 400" | STT got raw webm | ffmpeg transcode webm→wav before STT |
| "The agent says it sent something but didn't" | The brain narrated without invoking the tool | Narration is not action — the tool must be called |
| "The patch applied but nothing changed" | Stale running process | Restart, then `curl` the served page and grep |

## Conventions

- **Python:** 4-space indent, type hints where the codebase already
  uses them, no new dependencies without a reason.
- **Frontend:** the page is one embedded HTML string. There is no
  build step; don't introduce one.
- **Commits:** Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`,
  `chore:`).
- **Version bumps:** two places — `FastAPI(... version="X.Y.Z")` in
  `agent_voice_ai.py` and the `vX.Y.Z` string in the embedded HTML.
  Change both, then grep the served page to confirm.

## Things to do only with the operator's explicit approval

- **Push to a public remote.** This repo is published. Secrets must
  never reach a commit.
- **Add tools to `tools.py`.** The tool surface is curated. Adding
  `terminal`, `write_file`, or `patch` to a *voice* surface defeats
  the safety model — voice input is easy to misfire. Confirm first.
- **Change the confirmation protocol.** The tiered confirmation system
  (read-only auto / reversible auto / irreversible external held) is
  the safety-critical part of the tool surface.
- **Touch `certs/`.** TLS material is operator-managed; the bridge
  auto-detects whether certs exist.

## Where to read more

- `README.md` — install, configuration, phone access, protocol.
- `KNOWN_BUGS.md` — the open bug list, with workarounds.
- `FAQ.md` — the questions people actually ask.
- `CHANGELOG.md` — what changed, what broke, what was learned.
- `SECURITY.md` — threat model and secret-handling rules.
- `CONTRIBUTING.md` — commit conventions, code style.
