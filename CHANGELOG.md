# Changelog

All notable changes to **ai_agent_chat** are documented here. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); dates
are ISO 8601.

## [0.11.0] — 2026-10-27

### Added
- **The voice-channel gateway** (`skills_bridge.py`, renamed from
  `sam_zello_client.py`) — connects the agent to a voice channel over
  Tailscale, so it can be reached from a phone rather than only from the
  browser.
- **A social preview image** (`docs/social-preview.jpg`) for the README.

### Fixed
- **The API-key lookup chain works again.** The gateway could not find
  its key and fell back to the wrong source.
- **The gateway test was de-personalised** — it no longer depends on a
  specific person's environment to pass.
- **The social preview artwork was re-cropped** so nothing is cut off.

### Changed
- The README now opens in plain words rather than jargon, so the project
  is findable by someone searching for what it does.

## [0.10.9] — 2026-10-26

### Added
- **The build channel is stamped in the UI header**, so it is always
  obvious which deployment you are talking to.

### Fixed
- **The two voices no longer talk over each other.**
- **The brain retries transient provider failures** instead of dropping
  the turn.
- **The bridge's memory retrieval actually works** — it was reaching
  the wrong store.
- **The bridge routes to the real memories**, and puts the answer first
  rather than burying it.
- **A project question routes to recent activity**, not just to the
  inventory.
- **The model no longer re-reads memory it already has.**

## [0.10.8] — 2026-10-26

### Fixed
- **The top of the screen stays fixed on a phone.** The layout scrolled
  away under the on-screen keyboard.
- **The input box sits above the on-screen keyboard** instead of being
  hidden behind it.
- **The page no longer shakes when you hit Send.** It was the status
  text wrapping, which reflowed the layout on every message.
- **The screen no longer glitches on Send** — the app had been sized to
  the keyboard, so the viewport resized under the finger.

### Added
- **A layout test** (`c18c44e`) that proves the phone-layout fixes
  across iPhone viewports with the keyboard open.

## [0.10.7] — 2026-10-26

### Added
- **App logo in the header**, top-left, alongside the `ai_agent_chat`
  title.
- **Mute button moved into the stage**, next to the mic controls, so
  it's reachable in both push-to-talk and duplex.

### Changed
- The title is now `ai_agent_chat` (was `agent-voice-ai`).

## [0.10.6] — 2026-10-26

### Changed
- The hide-chat icon is now a chat bubble with an overlaid ✕, rather
  than a bare ✕. The restore icon is the same chat bubble without the
  ✕ — one icon family, so the control reads the same in both states.

## [0.10.5] — 2026-10-26

### Added
- **Mute button for duplex mode.** Disables the microphone track
  entirely — the OS stops capturing audio, not just the upload.

## [0.10.4] — 2026-10-26

### Fixed
- **The duplex interrupt now actually stops the agent talking.**

  This is the fix for *"it captures my interruption but doesn't stop
  talking — it finishes whatever it started before replying."*

  Root cause: the duplex reader treated `barge_stop` ("the user
  stopped talking") as "the turn is over" and exited. During the
  agent's reply there was therefore no reader consuming PCM, so the
  detector never saw the interruption at all. The agent finished its
  whole reply and only *then* captured what the user said.

  Fix: one reader owns the WebSocket for the entire duplex session.
  It never exits on `barge_stop`. The collect phase and the reply
  phase now share a single detector and a single mutable playback
  flag, so a trip detected during playback is visible to the reply
  handler and the brain stream is aborted.

  Verified: `phase=playback`, `[BARGE-ABORTED]`, and the
  interruption is transcribed and answered as the next turn.

## [0.10.3] — 2026-10-26

### Added
- The chat-bubble hide/unhide icon family.

### Fixed
- The microphone now releases correctly after a reply in duplex mode.

## [0.10.2] — 2026-10-26

### Fixed
- **Long replies were cut off.** Uvicorn's default `ws_ping_timeout` is
  20 seconds. A reply involving model thinking plus many TTS
  sentences exceeded it and the server closed the socket mid-answer —
  the user heard the reply cut off, then restart.

  Fix: `ws_ping_timeout=None`, plus `keepalive` frames sent between
  sentences so the connection stays warm during synthesis.

## [0.10.1] — 2026-10-26

### Fixed
- **The reply played twice.** The server sends a complete WAV as a
  Safari fallback for dropped frames, and the client was *also*
  playing the streamed audio. You heard the first part streamed, then
  the whole reply again from the fallback.

  Fix: the client counts WebSocket audio frames per turn; the
  fallback plays only if zero arrived. Regression test:
  `tests/test_no_double_play.py`.
- **The hide-chat button couldn't be restored.** The hide control lived
  inside the panel it hid. Once hidden there was no way back without
  reloading. Fix: the restore control is a sibling of the panel, not a
  child, so it stays reachable.

### Added
- `audio_url` and `audio_size` in duplex `done` payloads, so the
  fallback path works in duplex mode too.

## [0.10.0] — 2026-10-26

### Added
- **Real full-duplex voice mode.** This is the release that made the
  project worth using.

  - Persistent microphone in duplex mode. Once you tap the orb, the
    mic stays live — you never touch the screen again.
  - **Barge-in.** Talk over the agent and it stops mid-sentence.
  - The words you use to interrupt become the next request. You don't
    say them twice.
  - Server-side endpointing: the server decides where your utterance
    ended instead of relying on a client-side timer.
  - Auto-continue: after the agent's reply, the mic is already live
    again.

- **`barge.py`** — a self-contained voice-activity detector, ported
  from the Hermes agent framework's `tools/voice_mode.py` and adapted
  for audio arriving over a WebSocket. It's a deliberate port rather
  than an import so this repository stays standalone.
- **Abort-able brain streaming.** The WebSocket handler no longer
  blocks on the model, which is what makes interruption possible
  during generation.
- **The test suite.** Real end-to-end tests that drive a running
  bridge over a real WebSocket and assert on real behaviour — including
  speech tests that synthesize real audio and check the server
  transcribes the actual words.

### Changed
- The browser client gained a `ScriptProcessorNode` capture path
  (PCM16 mono @ 16 kHz, 30 ms frames) for duplex. The existing
  `MediaRecorder` push-to-talk path is unchanged.

## [0.9.1] — 2026-10-04

### Fixed
- iOS Safari `AbortError` / `NotAllowedError` silent spin. The play
  queue no longer retries in a tight loop on first failure; it sets a
  waiting-for-gesture flag and surfaces a pulsing "tap anywhere to
  resume audio" cue.

### Added
- New `tap-to-unlock` and `error` states on the status indicator.

## [0.9.0] — 2026-10-04

### Added
- **Hide-chat toggle** with a keyboard shortcut. State persists in
  `localStorage`.

## [0.8c] — 2026-10-04

### Fixed
- **"I said I sent but I didn't."** The agent could narrate "I sent it"
  without invoking the `send_message` tool. Fixed by adding an
  explicit pre-approval detector (`_has_pre_approval`, recognizing
  phrases like "fire away" / "no confirmation needed") and rewriting
  the system prompt to require tool invocation regardless.
- `NotAllowedError` handled as a sibling of `AbortError`, with the
  proper "tap to unlock" state transition.

## [0.8b] — 2026-10-04

### Fixed
- whisper.cpp returned HTTP 400 on raw webm bytes from `MediaRecorder`.
  Added an ffmpeg subprocess transcode (webm → wav) before the STT
  call.

## [0.8.0] — 2026-10-03

### Added
- **WebSocket `/ws` push-to-talk path**, replacing the legacy HTTP
  `/api/listen` + `/api/chat` round trip. Audio chunks in, text deltas
  and WAV chunks out.
- **Sentence-streaming TTS** — you hear sentence 1 while sentence 2
  is still being synthesized.
- **Anthropic prompt caching** — first-token time drops from ~2.5 s
  cold to ~700 ms warm.
- **launchd supervision** so the bridge survives reboots and restarts
  on crash.
- **Tiered confirmation protocol** — read-only tools auto-invoke,
  reversible writes auto-invoke and report, irreversible external
  writes are held for explicit confirmation.

### Notes
- The legacy HTTP paths remain in place for non-browser callers.

---

## Planned

- Wake-word detection (openWakeWord or Porcupine).
- Streaming STT to replace batch whisper.cpp.
- WebSocket authentication (`AVA_WS_TOKEN` + bearer check).
- Per-IP rate limiting, so a hostile client can't rack up LLM costs.
- A native wrapper app, so the mic can stay live with the screen off.
- Running the bridge as a low-privilege service account by default.
