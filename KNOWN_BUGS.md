# Known bugs

Open issues in `ai_agent_chat`. Ordered roughly by how likely you are
to hit them.

Status legend: **Open** — not fixed. **Fixed** — fixed in the noted
version; listed so you know it was a real problem and can recognise
the symptoms. **Won't fix** — deliberate decision, with reasoning.

---

## Open

### Long replies are slow

**Severity:** medium · **Affects:** everyone, worst on first turn

The agent's reply is synthesized sentence by sentence, and each
sentence waits for the previous one's audio to be prepared. On Kokoro
a sentence can take ~13 seconds to render. A five-sentence reply can
take a minute of wall-clock time before you hear the last word.

The WebSocket itself is not the bottleneck — it survives long replies
(see below). This is synthesis latency.

**Workaround:** keep `BRAIN_MAX_TOKENS` modest, and consider Piper
instead of Kokoro — Piper renders significantly faster for short
sentences.

**Not a bug, but worth knowing:** the first reply after a cold start
also includes model warm-up and possibly tool calls. That one is
usually much slower than subsequent turns.

---

### `BRAIN_MAX_TOKENS` truncates rather than continuing

**Severity:** medium · **Affects:** long-form answers

If a reply hits the token cap, it is cut off mid-sentence. There is no
automatic continuation.

**Workaround:** raise `BRAIN_MAX_TOKENS` if you need longer answers,
and expect longer synthesis latency (see above).

---

### The `playing` signal can be lost

**Severity:** low · **Affects:** duplex interruption reliability

The client tells the server when audio playback starts and stops, so
the detector can choose the right threshold for the current turn. If
those WebSocket messages are dropped, the server uses the wrong
threshold for that turn.

Symptoms: interruption works most of the time but occasionally doesn't
trip during playback.

**Workaround:** none needed in normal use. If you're seeing it
constantly, check the browser console for dropped frames.

---

### iOS Safari occasionally drops in-flight WebSocket frames

**Severity:** low · **Affects:** iOS Safari, long audio payloads

Large binary audio frames can be dropped by Safari on a flaky mobile
connection. The server sends a complete `/api/audio/<id>.wav` fallback
for exactly this case, so the audio still arrives — just later and
without streaming.

**Workaround:** already handled by the fallback path. If the fallback
also fails, the reply is still shown as text in the transcript.

---

### The detector is tuned for a quiet room

**Severity:** low · **Affects:** noisy environments

The default thresholds assume a reasonably quiet room. In a loud
environment the quiet floor is calibrated higher, which raises the
barge threshold. In a very quiet room, the floor can be low enough
that a small sound trips it.

**Workaround:** tune with `AVA_BARGE_TRACE=1` and watch the RMS
values in the log. Adjust `SILENCE_RMS_THRESHOLD` in `barge.py` and
`DEFAULT_BARGE_MULTIPLIER` accordingly. Turn the trace back off
afterwards.

---

### No wake word

**Severity:** feature gap · **Affects:** everyone

You must tap the orb to open the microphone. The mic does not open on
"hey agent" or similar.

**Not planned yet.** Adding a wake word means either running openWakeWord
or Porcupine locally and gating the capture stream. It's out of scope
until the duplex loop is stable enough that a false wake won't cause
noise.

---

### No push notifications

**Severity:** feature gap · **Affects:** everyone

If the agent sends you a message while the page is closed, you won't
know until you open it. Telegram integration can cover this if
configured.

---

### `MEMORY_*` paths default to another project's directories

**Severity:** low · **Affects:** anyone with those tools installed

The default memory paths point at `~/.config/agent-voice-ai/` and
similar. If you have another agent framework installed that owns those
paths, set the `MEMORY_*` variables explicitly rather than relying on
the defaults.

---

## Fixed

### WebSocket closed mid-reply on long answers

**Severity:** high · **Fixed in:** v0.10.2

Uvicorn's default `ws_ping_timeout` is 20 seconds. A long reply —
the model thinking for 30 seconds, then many TTS sentences — exceeded
it and the server closed the socket mid-answer. The user heard the
reply cut off and then restart.

**Fix:** `ws_ping_timeout=None`, plus `keepalive` frames sent between
sentences so the connection stays warm during synthesis.

---

### The reply played twice

**Severity:** high · **Fixed in:** v0.10.1

The server sends a complete WAV as a Safari fallback. The client was
also playing the streamed audio. You heard the first half of the reply
streamed, then the whole reply again from the fallback.

**Fix:** the client counts WebSocket audio frames per turn. The
fallback plays **only** if zero arrived. Regression test:
`tests/test_no_double_play.py`.

---

### The duplex reader died on `barge_stop`

**Severity:** high · **Fixed in:** v0.10.4

The duplex reader treated `barge_stop` — "the user stopped talking" —
as "the turn is over" and exited. During the agent's reply there was
therefore no reader consuming PCM, so the detector never saw the
interruption. The agent would finish its whole reply, and only then
capture what the user said. From the user's side: "it captures my
interruption but doesn't stop talking."

**Fix:** one reader owns the socket for the entire duplex session. It
never exits on `barge_stop`.

---

### The hide-chat button could not be restored

**Severity:** medium · **Fixed in:** v0.10.1

The hide button lived inside the panel it hid. Once hidden, there was
no way to bring the chat back without reloading the page.

**Fix:** the restore control is now a sibling of the panel, not a
child, so it stays reachable when the panel is hidden. The icons are
consistent: 💬 to show, 💬 with a cross to hide.

---

### The agent never interrupted during generation

**Severity:** high · **Fixed in:** v0.10.0

The WebSocket handler awaited the complete brain response before
reading the next frame, so no audio could arrive while the model was
thinking. Interruption was impossible during generation — only
possible if the timing happened to line up.

**Fix:** the brain stream is abortable, and a background reader
consumes frames concurrently.

---

### The microphone didn't release after a reply

**Severity:** medium · **Fixed in:** v0.10.3

After the agent finished speaking in duplex mode, the capture track
stayed live but the UI still showed the mic as busy — you couldn't
start your next turn.

**Fix:** one continuous duplex session owns one capture stream for its
whole lifetime. The mic stays open by design; the UI state now
matches reality.

---

### whisper.cpp returned HTTP 400 on webm audio

**Severity:** medium · **Fixed in:** v0.8b

The browser sends webm/Opus. whisper.cpp's middleware checks for
`audio/wav` and 400s on anything else.

**Fix:** ffmpeg transcodes webm→wav before the STT call.

---

### The agent narrated sending a message without sending it

**Severity:** high · **Fixed in:** v0.8c

The brain would say "I sent it" without invoking the tool. The
tiered-confirmation protocol was added: read-only tools auto-invoke,
reversible writes auto-invoke and report, irreversible external writes
(sending a Telegram message) are held for explicit confirmation.

**Fix:** pre-approval phrases ("just send it", "fire away") auto-confirm
the tool — but never let the model skip calling it. Narration is not
action.

---

### Safari's AbortError caused a silent audio spin

**Severity:** medium · **Fixed in:** v0.9.1

Safari rejects `audio.play()` until the user taps after the device
locks. The play queue re-called itself in a tight loop on AbortError,
burning CPU and producing no sound.

**Fix:** `_wsWaitingForGesture` early-return plus a visible "tap
anywhere to resume audio" cue.

---

## Won't fix

### The frontend is one embedded HTML string in a Python file

**Why:** no build step, no asset pipeline, no Node toolchain, and the
whole app is a single `git clone` + `pip install`. Introducing a
frontend build would make the project meaningfully harder to install
and deploy for most self-hosters, in exchange for developer
convenience that a handful of contributors need.

The file is large. It is still one module with clear sections.

---

### There's no authentication on the bridge

**Why:** this is a single-user tool that is meant to run on a trusted
network — normally a tailnet. Adding auth would mean managing
credentials, which is a worse failure mode than "the VPN is your
access control."

**Do not port-forward it to the internet.** See
[SECURITY.md](SECURITY.md).

---

### Voice input doesn't get the full tool surface

**Why:** mishearing is common. A misheard "delete" shouldn't delete
files. The tool surface is curated for voice: read-only operations are
safe, irreversible external writes require confirmation, and broad
filesystem or shell access is deliberately absent.

If you need more tools, prefer adding a narrow, specific one over
exposing a general shell.
