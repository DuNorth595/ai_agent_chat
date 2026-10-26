#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""
agent_voice_ai.py — The agent-voice-ai bridge.

FastAPI server on port 8770 (configurable via AVA_PORT env).
Endpoints:
  GET  /              → minimal web UI (HTML)
  GET  /api/health    → health check
  POST /api/chat      → text in, text + audio out
  POST /api/listen    → audio in (WAV), text + audio out
  WS   /ws            → push-to-talk: audio chunks in, sentence-streaming text + audio out
  GET  /api/history   → get session history
  POST /api/clear     → clear session

Audio out: 24kHz mono WAV (Kokoro).
Audio in:  16kHz mono WAV (whisper-server default).

Run:    python agent_voice_ai.py
Debug:  AGENT_VOICE_VERBOSE=1 python agent_voice_ai.py
"""
import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path, override=False)
        logging.getLogger("agent_voice_ai").info("loaded env from %s", _env_path)
except ImportError:
    pass  # python-dotenv not installed — rely on EnvironmentVariables / process env

import brain
import barge  # barge-in (full-duplex) voice detection — port of Hermes _BargeDetector
try:
    import kokoro_tts  # optional fallback TTS provider; may be unavailable on this box
    _KOKORO_OK = True
    KOKORO_SAMPLE_RATE = 24000  # Kokoro 82M is fixed at 24kHz mono PCM_16
except Exception as _kokoro_err:
    kokoro_tts = None  # type: ignore[assignment]
    _KOKORO_OK = False
    KOKORO_SAMPLE_RATE = 24000
    logging.getLogger("agent_voice_ai").warning("kokoro_tts unavailable: %s — fallback chain disabled", _kokoro_err)
import piper_tts
import whisper_stt
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response

# ---- logging ----
logging.basicConfig(
    level=logging.DEBUG if os.environ.get("AGENT_VOICE_VERBOSE") else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("agent_voice_ai.bridge")

# ---- app ----
# Version + channel live in one place so the HTML header and the
# /api/health payload can never drift apart.
#
# AVA_CHANNEL is "prod" or "dev". The dev instance is identified by
# its port (8771) and by AVA_CHANNEL; the label is shown next to the
# version in the top-right of the UI so it is always obvious which
# build you are talking to.
APP_VERSION = os.environ.get("AVA_VERSION", "0.10.7")
APP_CHANNEL = os.environ.get("AVA_CHANNEL", "prod").strip().lower()
if APP_CHANNEL not in ("prod", "dev"):
    APP_CHANNEL = "prod"

app = FastAPI(title="ai_agent_chat", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("AVA_CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

PORT = int(os.environ.get("AVA_PORT", "8770"))
HOST = os.environ.get("AVA_HOST", "0.0.0.0")  # bind 0.0.0.0 so any LAN interface (or VPN/Tailscale) can reach it


def _synth_with_fallback(reply_text: str) -> tuple[bytes, str]:
    """
    Synthesize reply_text → WAV bytes synchronously using the active TTS
    provider chain. Caller must be inside an async handler; we use
    run_in_executor at the call site to keep the event loop responsive.
    """
    tts_provider = os.environ.get("AGENT_VOICE_TTS_PROVIDER", "piper").lower()
    # Primary: Piper (the default voice-bridge TTS)
    if tts_provider == "piper" and piper_tts.is_available():
        return piper_tts.synth_to_wav_bytes(reply_text), "piper"
    # Fallback: Kokoro (only if it actually imported)
    if _KOKORO_OK and kokoro_tts is not None:
        return kokoro_tts.synth_to_wav_bytes(reply_text), "kokoro"
    raise RuntimeError("no TTS provider available (piper + kokoro both unavailable)")


# Pre-warm Kokoro on startup so the first /api/chat call doesn't pay the ~5s
# model-load penalty. Without this, the first request to a freshly-restarted
# bridge takes ~13s and the mobile client times out before the audio_url
# is returned (which is the "50% silent audio" bug we just diagnosed).
_warmup_done = False
def _warm_kokoro():
    global _warmup_done
    if _warmup_done:
        return
    if not _KOKORO_OK or kokoro_tts is None:
        return  # kokoro not available, nothing to warm
    try:
        # synth a throwaway string to load the model + voice
        kokoro_tts.synth_to_wav_bytes("ready.")  # voice resolved at call time from KOKORO_VOICE env
        _warmup_done = True
        log.info("kokoro pre-warmed")
    except Exception as e:
        log.warning("kokoro warmup failed: %s — bridge will fall back at runtime", e)


# ---- web UI ----
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0a0e1a">
<title>ai_agent_chat</title>
<style>
  :root {
    --bg: #0a0e1a;
    --bg-elev: #131a2a;
    --text: #e8eef9;
    --text-dim: #6b7a99;
    --accent: #3b82f6;
    --accent-glow: rgba(59, 130, 246, 0.5);
    --state-idle: #2a3550;
    --state-listening: #3b82f6;
    --state-thinking: #f59e0b;
    --state-speaking: #10b981;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body {
    margin: 0; padding: 0;
    background: var(--bg);
    color: var(--text);
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Display", system-ui, sans-serif;
    height: 100%; overflow: hidden;
  }
  body {
    display: flex; flex-direction: column;
    padding: env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
  }
  .header {
    display: flex; align-items: center; gap: 12px;
    padding: 16px 20px;
    border-bottom: 1px solid var(--bg-elev);
  }
  .header h1 {
    margin: 0; font-size: 18px; font-weight: 600; letter-spacing: 0.05em;
  }
  /* v0.10.7 — the app logo, top-left of the header. The source is a
     square PNG on a black ground, so it sits flush against the dark
     header without a visible edge. */
  .app-logo {
    width: 28px; height: 28px;
    border-radius: 6px;
    display: block;
    flex: 0 0 auto;
  }
  .header .status {
    font-size: 12px; color: var(--text-dim);
    margin-left: auto;
  }
  .header .status .dot {
    display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    background: var(--state-idle);
    margin-right: 6px;
    vertical-align: middle;
  }
  .stage {
    flex: 1;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    padding: 20px;
    position: relative;
  }
  .orb {
    width: 200px; height: 200px;
    border-radius: 50%;
    background: radial-gradient(circle at 30% 30%, var(--accent), #1e3a8a);
    box-shadow: 0 0 60px var(--accent-glow);
    display: flex; align-items: center; justify-content: center;
    transition: all 0.3s ease;
    cursor: pointer;
    user-select: none;
    -webkit-user-select: none;
  }
  .orb.listening {
    background: radial-gradient(circle at 30% 30%, #60a5fa, #3b82f6);
    box-shadow: 0 0 80px var(--accent-glow);
    animation: pulse 1.2s ease-in-out infinite;
  }
  /* ── v0.10 duplex UI ────────────────────────────────────────────── */
  /* Mode toggle: PTT ↔ Duplex. Duplex keeps the mic hot so you can
     talk over Sam without touching the screen. */
  .mode-toggle {
    display: flex; gap: 2px; padding: 3px; margin-bottom: 16px;
    background: var(--bg-elev); border-radius: 999px;
    border: 1px solid rgba(255,255,255,0.08);
  }
  .mode-toggle button {
    border: none; background: transparent; color: var(--text-dim);
    padding: 7px 18px; border-radius: 999px; font-size: 13px;
    font-weight: 600; cursor: pointer; font-family: inherit;
    transition: all 0.2s ease; white-space: nowrap;
  }
  .mode-toggle button.active {
    background: var(--accent); color: #fff;
    box-shadow: 0 2px 8px var(--accent-glow);
  }
  .mode-toggle button.duplex.active {
    background: #10b981; box-shadow: 0 2px 8px rgba(16,185,129,0.5);
  }
  /* Live mic level meter — shows you're being heard in duplex mode. */
  .vu {
    width: 220px; height: 5px; margin-top: 18px;
    background: rgba(255,255,255,0.08); border-radius: 999px;
    overflow: hidden; opacity: 0; transition: opacity 0.25s ease;
  }
  .vu.active { opacity: 1; }
  .vu-fill {
    height: 100%; width: 0%;
    background: linear-gradient(90deg, #10b981, #fbbf24 70%, #ef4444);
    border-radius: 999px; transition: width 0.06s linear;
  }
  /* In duplex mode the orb is a status indicator, not a button. */
  .stage.duplex .orb { cursor: default; }
  .stage.duplex .orb.interrupted {
    background: radial-gradient(circle at 30% 30%, #fbbf24, #b45309);
    box-shadow: 0 0 70px rgba(251,191,36,0.5);
  }
  .duplex-hint {
    margin-top: 10px; font-size: 12px; color: var(--text-dim);
    text-align: center; max-width: 260px; line-height: 1.4;
  }
  .duplex-hint b { color: #10b981; }
  .orb.thinking {
    background: radial-gradient(circle at 30% 30%, #fbbf24, #f59e0b);
    box-shadow: 0 0 80px rgba(245, 158, 11, 0.5);
    animation: spin 2s linear infinite;
  }
  .orb.speaking {
    background: radial-gradient(circle at 30% 30%, #34d399, #10b981);
    box-shadow: 0 0 80px rgba(16, 185, 129, 0.5);
  }
  @keyframes pulse {
    0%, 100% { transform: scale(1); }
    50% { transform: scale(1.08); }
  }
  @keyframes spin {
    0% { transform: rotate(0deg); }
    100% { transform: rotate(360deg); }
  }
  .orb-label {
    margin-top: 24px;
    font-size: 14px;
    color: var(--text-dim);
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }
  .transcript {
    flex: 0 0 auto;
    padding: 20px;
    overflow-y: auto;
    max-height: 40vh;
    border-top: 1px solid var(--bg-elev);
  }
  .turn { margin-bottom: 16px; }
  .turn .role {
    font-size: 11px; color: var(--text-dim);
    text-transform: uppercase; letter-spacing: 0.1em;
    margin-bottom: 4px;
  }
  .turn.user .role { color: #60a5fa; }
  .turn.assistant .role { color: #34d399; }
  .turn .text { font-size: 15px; line-height: 1.5; }
  /* v0.10: a reply the user interrupted mid-sentence */
  .turn.sam.interrupted .text { opacity: 0.55; }
  .turn.sam.interrupted .role::after { content: " — interrupted"; color: #fbbf24; }
  .empty { color: var(--text-dim); text-align: center; padding: 20px; font-style: italic; }
  .controls {
    padding: 16px 20px;
    border-top: 1px solid var(--bg-elev);
    display: flex; gap: 12px; align-items: center;
  }
  button {
    background: var(--accent);
    color: white;
    border: none;
    padding: 12px 20px;
    border-radius: 8px;
    font-size: 14px; font-weight: 500;
    cursor: pointer;
    -webkit-appearance: none;
  }
  button.secondary {
    background: var(--bg-elev);
    color: var(--text-dim);
  }
  button:active { opacity: 0.7; }
  input[type="text"] {
    flex: 1;
    background: var(--bg-elev);
    color: var(--text);
    border: 1px solid transparent;
    padding: 12px 16px;
    border-radius: 8px;
    font-size: 15px;
    outline: none;
  }
  input[type="text"]:focus { border-color: var(--accent); }
  .mic-pulse { display: none; }
  .mic-pulse.active { display: inline-block; animation: pulse 1s ease-in-out infinite; }

  /* v0.9 hide-chat */
  .chat-shell { position: relative; }
  .chat-bar { display: flex; justify-content: flex-end; align-items: center; margin: 0 0 6px 0; }
  .hide-btn {
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12);
    color: var(--text); font-size: 14px; line-height: 1; padding: 6px 10px;
    border-radius: 8px; cursor: pointer; user-select: none;
    position: relative;   /* v0.10.6 — the X overlays the bubble */
  }
  .hide-btn:hover { background: rgba(255,255,255,0.12); }
  .hide-btn.shown { opacity: 0.5; }
  /* v0.10.6 — the "hide chat" affordance is the chat bubble with a
     small red X drawn ON TOP of it, not a second glyph beside it.
     "💬❌" renders as two side-by-side emoji; this stacks them. */
  .hide-btn .bubble { font-size: 14px; line-height: 1; }
  .hide-btn .x-mark {
    position: absolute;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%) scale(0.62);
    font-size: 14px;
    line-height: 1;
    pointer-events: none;   /* clicks go to the button */
    filter: drop-shadow(0 0 2px rgba(0,0,0,0.9));
  }
  .chat-shell.hidden .transcript,
  .chat-shell.hidden .controls { display: none !important; }
  .chat-shell.hidden .chat-bar { margin-top: 0; }
  .chat-shell.hidden { padding-bottom: 0; }
  body.chat-hidden .chat-shell { display: none; }
  /* v0.10.6 — mute button. Sits in the stage flow, directly under the
     duplex hint ("Mic is live. Talk any time..."). Only shown in duplex
     mode, where the mic is always live. */
  .mute-btn {
    margin-top: 14px;
    background: rgba(19,26,42,0.92); border: 1px solid rgba(255,255,255,0.18);
    color: var(--text); font-size: 15px; line-height: 1;
    padding: 9px 14px; border-radius: 999px; cursor: pointer;
    user-select: none; -webkit-user-select: none;
    box-shadow: 0 4px 16px rgba(0,0,0,0.45);
    display: none;
  }
  .mute-btn.active {
    background: rgba(239,68,68,0.92); border-color: rgba(255,255,255,0.3);
    color: #fff;
  }
  body.duplex-mode .mute-btn { display: inline-block; }
  /* v0.10.3 — the unhide affordance lives OUTSIDE the chat shell.
     Hiding the shell used to hide the hide button with it, so once you
     hid the chat you could never bring it back (the button is a child
     of the element it hides). This button is a sibling, not a child. */
  .show-chat-btn {
    position: fixed; top: 10px; right: 10px; z-index: 1200;
    background: rgba(19,26,42,0.92); border: 1px solid rgba(255,255,255,0.18);
    color: var(--text); font-size: 15px; line-height: 1;
    padding: 9px 12px; border-radius: 10px; cursor: pointer;
    user-select: none; -webkit-user-select: none;
    box-shadow: 0 4px 16px rgba(0,0,0,0.45);
    display: none;
  }
  .show-chat-btn:active { background: rgba(59,130,246,0.3); }
  body.chat-hidden .show-chat-btn { display: block; }
</style>
</head>
<body>
  <div class="header">
    <img class="app-logo" src="/static/logo.jpg" alt="Sam logo" width="28" height="28">
    <h1>ai_agent_chat</h1>
    <div class="status"><span class="dot" id="status-dot"></span><span id="status-text">idle</span></div>
    <div class="version" id="build-version" style="font-size:10px;color:#4a5a7a;margin-top:2px;">v__VERSION__ __CHANNEL__</div>
  </div>

  <!-- v0.10.1 — unhide button. Deliberately OUTSIDE .chat-shell so that
       hiding the chat cannot hide the control that brings it back.
       v0.10.3 — same chat-bubble icon as the hide button, so the two
       read as the same control in two states. -->
  <button class="show-chat-btn" id="show-chat-btn" title="Show chat" aria-label="Show chat">💬</button>

  <div class="stage" id="stage">
    <div class="mode-toggle" id="mode-toggle">
      <button id="mode-ptt" class="active">Push to talk</button>
      <button id="mode-duplex" class="duplex">Duplex</button>
    </div>
    <div class="orb" id="orb"></div>
    <div class="orb-label" id="orb-label">tap to talk</div>
    <div class="vu" id="vu"><div class="vu-fill" id="vu-fill"></div></div>
    <div class="duplex-hint" id="duplex-hint"></div>
    <!-- v0.10.6 — mute button. Only shown in duplex mode, where the mic
         is always live. Clicking it stops Sam hearing you; the mic track
         is disabled so the OS stops capturing, not just the upload.
         Sits directly under the duplex hint. -->
    <button class="mute-btn" id="mute-btn" title="Mute mic" aria-label="Mute mic">🎙️</button>
  </div>

  <div class="chat-shell" id="chat-shell">
    <div class="chat-bar">
      <button class="hide-btn" id="hide-btn" title="Hide chat (Cmd+H)" aria-label="Hide chat"><span class="bubble">💬</span><span class="x-mark">❌</span></button>
    </div>
    <div class="transcript" id="transcript">
      <div class="empty">conversation will appear here</div>
    </div>

    <div class="controls">
      <input type="text" id="text-input" placeholder="message sam..." autocomplete="off">
      <button id="send-btn">Send</button>
      <button class="secondary" id="clear-btn" title="Clear conversation">×</button>
    <button class="secondary" id="debug-btn" title="View debug log">🪲</button>
    <button class="secondary" id="audio-test-btn" title="Test audio output (plays a 440Hz tone)">🔊</button>
  </div>
  <div id="debug-panel" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:2000;padding:20px;overflow:auto;font-family:monospace;font-size:12px;color:#a7f3d0;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
      <strong>ai_agent_chat debug log</strong>
      <button id="debug-close" style="background:#374151;color:#fff;border:none;padding:6px 12px;border-radius:6px;">close</button>
    </div>
    <pre id="debug-log" style="white-space:pre-wrap;line-height:1.4;"></pre>
  </div>

<script>
const SESSION_ID = "ava-" + (localStorage.getItem("ava-session") || (() => {
  const id = Math.random().toString(36).slice(2, 10);
  localStorage.setItem("ava-session", id);
  return id;
})());

const orb = document.getElementById("orb");
const orbLabel = document.getElementById("orb-label");
const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const transcript = document.getElementById("transcript");
const textInput = document.getElementById("text-input");
const hideBtn = document.getElementById("hide-btn");
const chatShell = document.getElementById("chat-shell");

// v0.9 hide-chat toggle (persisted)
const HIDE_KEY = "sb_hide_chat_v1";
const showChatBtn = document.getElementById("show-chat-btn");
function applyHide(hidden) {
  document.body.classList.toggle("chat-hidden", hidden);
  // v0.10.6 — the hide affordance is the chat bubble with a small red
  // X OVERLAID on it. The button's contents are static markup (a .bubble
  // span and an .x-mark span), so we must NOT set textContent here —
  // that would destroy the spans.
  //
  // The in-shell button only ever needs the HIDE affordance — when the
  // chat is hidden the whole shell (including this button) is
  // display:none, so the floating button is what you see, and it always
  // shows the plain bubble.
  hideBtn.title = "Hide chat (Cmd+H)";
  // v0.10.1 — the floating button is the way back when the chat is
  // hidden. It lives outside .chat-shell precisely so that hiding the
  // chat cannot hide the control that unhides it.
  if (showChatBtn) {
    showChatBtn.textContent = "💬";
    showChatBtn.title = "Show chat";
  }
  // v0.10.6 — the X is a separate element layered over the bubble, so
  // the plain bubble shows through. Nothing to reset here, but make
  // sure the button has its markup if it was ever rebuilt.
  if (!hideBtn.querySelector(".x-mark")) {
    hideBtn.innerHTML =
      '<span class="bubble">💬</span><span class="x-mark">❌</span>';
  }
}
applyHide(localStorage.getItem(HIDE_KEY) === "1");
hideBtn.addEventListener("click", () => {
  const next = !document.body.classList.contains("chat-hidden");
  localStorage.setItem(HIDE_KEY, next ? "1" : "0");
  applyHide(next);
});
// v0.10.1 — the floating unhide button.
if (showChatBtn) {
  showChatBtn.addEventListener("click", () => {
    localStorage.setItem(HIDE_KEY, "0");
    applyHide(false);
  });
}
document.addEventListener("keydown", (e) => {
  const mod = e.metaKey || e.ctrlKey;
  if (mod && (e.key === "h" || e.key === "H")) {
    e.preventDefault();
    hideBtn.click();
  }
});
const sendBtn = document.getElementById("send-btn");
const clearBtn = document.getElementById("clear-btn");

// ── v0.10 duplex mode ──────────────────────────────────────────────
//
// Duplex mode is the "real full duplex" experience: the mic stays hot
// for the whole conversation, you never touch the screen, and you can
// talk over Sam mid-sentence. Sam hears you, stops, and answers what
// you said.
//
// PTT mode is unchanged — tap the orb, talk, release.
const stageEl = document.getElementById("stage");
const modePttBtn = document.getElementById("mode-ptt");
const modeDuplexBtn = document.getElementById("mode-duplex");
const vuEl = document.getElementById("vu");
const vuFill = document.getElementById("vu-fill");
const duplexHint = document.getElementById("duplex-hint");

let _mode = "ptt";              // "ptt" | "duplex"
let _duplexStream = null;      // the live MediaStream in duplex mode
let _duplexAnalyser = null;    // for the VU meter
let _duplexRaf = null;         // VU animation frame
let _duplexActive = false;     // is duplex currently running?

let state = "idle";
let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;

// ── v0.10: continuous PCM capture for barge-in ──────────────────────
//
// The MediaRecorder path (webm/opus) is for the normal push-to-talk STT
// and stays exactly as it was. For barge-in we need raw PCM16 frames
// the server can run a VAD on, so we tap the SAME AudioContext with a
// ScriptProcessorNode and ship 30ms frames over the same WebSocket.
//
// ScriptProcessorNode is deprecated in favour of AudioWorklet, but it
// is the only one that works reliably on iOS Safari 14+ without a
// separate module file (which this single-file server can't serve
// cleanly). 4096-frame buffer = 256ms latency at 16kHz; we use
// 2048 to halve that.
let _pcmSource = null;
let _pcmProc = null;
let _bargeEnabled = false;
let _bargeFrames = [];
const BARGE_FRAME_SAMPLES = 480;   // 30ms @ 16kHz
const BARGE_FRAME_MS = 30;

function _bargePcmEnabled() {
  return _bargeEnabled && _ws && _ws.readyState === 1;
}

// Attach a PCM tap to the live mic stream. Called once per WS session.
function _attachPcmTap(stream) {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    if (!_bargeCtx) {
      _bargeCtx = new Ctx({ sampleRate: 16000, latencyHint: "interactive" });
    }
    const ctx = _bargeCtx;
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    _pcmSource = ctx.createMediaStreamSource(stream);
    _pcmProc = ctx.createScriptProcessor(2048, 1, 1);
    _bargeFrames = [];
    _pcmProc.onaudioprocess = (ev) => {
      if (!_bargePcmEnabled()) return;
      const input = ev.inputBuffer.getChannelData(0);
      // Accumulate into 30ms (480-sample) frames and ship each one.
      for (let i = 0; i < input.length; i++) _bargeBuf.push(input[i]);
      while (_bargeBuf.length >= BARGE_FRAME_SAMPLES) {
        const frame = _bargeBuf.splice(0, BARGE_FRAME_SAMPLES);
        _sendBargeFrame(frame);
        // v0.10: feed the level to the duplex VU/endpointer. The RMS is
        // computed on the float frame BEFORE int16 conversion, so it is
        // in the -1..1 domain.
        if (_mode === "duplex") {
          let sum = 0;
          for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
          _duplexLevel(Math.sqrt(sum / frame.length));
        }
      }
    };
    _pcmSource.connect(_pcmProc);
    // A ScriptProcessor only fires if connected to a destination. Use a
    // zero-gain node so the mic isn't echoed back to the speaker.
    if (!_bargeSilent) {
      _bargeSilent = ctx.createGain();
      _bargeSilent.gain.value = 0;
      _pcmProc.connect(_bargeSilent);
      _bargeSilent.connect(ctx.destination);
    }
  } catch (e) {
    dbg("barge pcm tap failed: " + e.message);
  }
}

let _bargeCtx = null;
let _bargeSilent = null;
let _bargeBuf = [];

function _sendBargeFrame(floatFrame) {
  // Float32 [-1,1] → int16 PCM.
  const n = floatFrame.length;
  const buf = new ArrayBuffer(n * 2);
  const view = new DataView(buf);
  for (let i = 0; i < n; i++) {
    let s = Math.max(-1, Math.min(1, floatFrame[i]));
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  let bin = "";
  const bytes = new Uint8Array(buf);
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  _ws.send(JSON.stringify({
    type: "pcm",
    data_b64: btoa(bin),
  }));
}

function _detachPcmTap() {
  try {
    if (_pcmSource) { _pcmSource.disconnect(); _pcmSource = null; }
    if (_pcmProc) { _pcmProc.onaudioprocess = null; _pcmProc.disconnect(); _pcmProc = null; }
    _bargeBuf = [];
  } catch (e) { /* ignore */ }
}

// ── v0.10: duplex mode ──────────────────────────────────────────────
//
// The duplex loop, from the phone's side:
//   1. Mic stays hot the whole time (no button, no PTT).
//   2. We ship raw PCM frames to the server continuously.
//   3. The server's VAD decides when we're actually SPEAKING (not
//      echoing Sam's own voice off the speaker).
//   4. When we stop speaking for ~700ms we send {"type":"barge_stop"}.
//   5. The server endpoints, transcribes, and runs our turn.
//
// Step 4 is the piece that makes it conversational rather than
// half-duplex: without it the server has no idea when our sentence
// ended.

// How long we stay quiet before we consider ourselves done talking.
const DUPLEX_SILENCE_MS = 700;
// Minimum speech length before we bother the server.
const DUPLEX_MIN_SPEECH_MS = 400;
// RMS above this counts as "we are talking" (float32 -1..1 domain).
const DUPLEX_SPEECH_RMS = 0.02;

let _duplexSpeaking = false;
let _duplexSpeechStart = 0;
let _duplexLastVoice = 0;
let _duplexSpeechMs = 0;
let _duplexLastFrameAt = 0;

// Called from the PCM tap with the RMS of each 30ms frame.
function _duplexLevel(rms) {
  const now = performance.now();
  _duplexLastFrameAt = now;
  // VU meter: map RMS to a 0-100% bar (log-ish, since speech RMS is small).
  const pct = Math.max(0, Math.min(100, (Math.log10(Math.max(rms, 1e-5)) + 3) * 26));
  vuFill.style.width = pct.toFixed(1) + "%";

  if (rms > DUPLEX_SPEECH_RMS) {
    if (!_duplexSpeaking) {
      _duplexSpeaking = true;
      _duplexSpeechStart = now;
      _duplexSpeechMs = 0;
      dbg("duplex: speech started");
    }
    _duplexLastVoice = now;
    _duplexSpeechMs = now - _duplexSpeechStart;
  } else if (_duplexSpeaking) {
    // We went quiet. If we've been quiet long enough, we're done.
    if (now - _duplexLastVoice > DUPLEX_SILENCE_MS) {
      _duplexSpeaking = false;
      const dur = _duplexSpeechMs;
      _duplexSpeechMs = 0;
      if (dur >= DUPLEX_MIN_SPEECH_MS) {
        dbg("duplex: utterance ended after " + Math.round(dur) + "ms — sending barge_stop");
        if (_ws && _ws.readyState === 1) {
          _ws.send(JSON.stringify({ type: "barge_stop" }));
        }
        setState("thinking");
      } else {
        dbg("duplex: too short (" + Math.round(dur) + "ms) — ignored");
      }
    }
  }
}

// Start duplex mode: open the mic and keep it open.
async function _duplexStart() {
  if (_duplexActive) return;
  try {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showHint("This browser doesn't support microphone access.");
      return;
    }
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!_bargeCtx) {
      _bargeCtx = new Ctx({ sampleRate: 16000, latencyHint: "interactive" });
    }
    if (_bargeCtx.state === "suspended") await _bargeCtx.resume();

    _duplexStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, sampleRate: 16000, echoCancellation: true, noiseSuppression: true }
    });

    // Open the WS and attach the PCM tap.
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    _ws = new WebSocket(proto + "//" + window.location.host + "/ws");
    _wsSentences = [];
    _wsPlaying = false;
    _wsCurrentReplyEl = null;
    _textDbgFired = false;

    await new Promise((resolve, reject) => {
      _ws.onopen = () => {
        // "duplex" (not "start") puts the server into its persistent
        // collect → transcribe → answer loop.
        _ws.send(JSON.stringify({ type: "duplex", session_id: SESSION_ID }));
        _attachPcmTap(_duplexStream);
        _bargeEnabled = true;   // stream PCM for the whole session
        resolve();
      };
      _ws.onerror = () => reject(new Error("ws connect failed"));
      setTimeout(() => reject(new Error("ws timeout")), 4000);
    });

    _ws.onmessage = _wsMessageHandler;
    _ws.onclose = () => {
      dbg("ws closed");
      _ws = null;
      if (_mode === "duplex") {
        // Auto-reconnect so a backgrounded phone doesn't kill the session.
        setTimeout(() => { if (_mode === "duplex" && !_ws) _duplexStart(); }, 1200);
      } else if (state === "listening" || state === "thinking") {
        _wsReset();
      }
    };

    _duplexActive = true;
    vuEl.classList.add("active");
    stageEl.classList.add("duplex");
    setState("listening");
    orbLabel.textContent = "listening — just talk";
    duplexHint.innerHTML = "Mic is <b>live</b>. Talk any time — you can interrupt Sam mid-sentence.";
  } catch (e) {
    showHint("Duplex mic failed: " + e.message + ". Falling back to push-to-talk.");
    _setMode("ptt");
  }
}

// Stop duplex mode and release the mic.
function _duplexStop() {
  _duplexActive = false;
  _bargeEnabled = false;
  vuEl.classList.remove("active");
  stageEl.classList.remove("duplex");
  vuFill.style.width = "0%";
  _detachPcmTap();
  try {
    if (_duplexStream) {
      _duplexStream.getTracks().forEach(t => t.stop());
      _duplexStream = null;
    }
  } catch (e) { /* ignore */ }
  try {
    if (_ws) {
      if (_ws.readyState === 1) {
        _ws.send(JSON.stringify({ type: "duplex_off" }));
      }
      _ws.close();
      _ws = null;
    }
  } catch (e) { /* ignore */ }
  _duplexSpeaking = false;
  _duplexSpeechMs = 0;
}

// ── v0.10.5: mute ────────────────────────────────────────────────────
//
// In duplex the mic is always live, so there is no natural "stop
// talking" gesture. The mute button gives you one: Sam stops hearing
// you, without leaving the page and without tearing down the session.
//
// Two things have to happen:
//   1. The mic TRACK is disabled, so the OS stops capturing audio at
//      all. Muting only the upload would still leave the phone's mic
//      hot — the privacy difference is real, and so is the battery.
//   2. The server is told, so it does not treat the silence as "you
//      stopped talking" and start a turn on stale audio.
let _muted = false;

function _setMute(muted) {
  _muted = muted;
  document.body.classList.toggle("muted", muted);
  const btn = document.getElementById("mute-btn");
  if (btn) {
    btn.classList.toggle("active", muted);
    btn.textContent = muted ? "🔇" : "🎙️";
    btn.title = muted ? "Unmute mic" : "Mute mic";
  }
  // Disable/enable the actual capture track.
  try {
    if (_duplexStream) {
      _duplexStream.getAudioTracks().forEach((t) => {
        t.enabled = !muted;
      });
    }
  } catch (e) { /* ignore */ }
  // Stop shipping PCM while muted. Scoped to duplex: in PTT mode
  // _bargeEnabled is false already (the orb drives capture), and
  // un-muting must not switch PTT on by accident.
  if (_mode === "duplex") {
    _bargeEnabled = !muted;
  }
  // Tell the server, so it doesn't treat the gap as end-of-speech.
  try {
    if (_ws && _ws.readyState === 1) {
      _ws.send(JSON.stringify({ type: "mute", value: muted }));
    }
  } catch (e) { /* ignore */ }
  dbg("mute: " + (muted ? "on" : "off"));
  if (muted) {
    // Reset the speech detector so the un-muted session starts clean.
    _duplexSpeaking = false;
    _duplexSpeechMs = 0;
    _bargeBuf = [];
  }
}

function _setMode(m) {
  if (_mode === m) return;
  if (m === "duplex") {
    _mode = "duplex";
    modePttBtn.classList.remove("active");
    modeDuplexBtn.classList.add("active");
    // v0.10.5 — the mute button only exists in duplex (in PTT you
    // already have the orb as a push-to-talk control).
    document.body.classList.add("duplex-mode");
    orbLabel.textContent = "starting mic…";
    _duplexStart();
  } else {
    _mode = "ptt";
    modeDuplexBtn.classList.remove("active");
    modePttBtn.classList.add("active");
    document.body.classList.remove("duplex-mode");
    duplexHint.textContent = "";
    // Leaving duplex — unmute, so the next duplex session starts live.
    if (_muted) _setMute(false);
    _duplexStop();
    orbLabel.textContent = "tap to talk";
    setState("idle");
  }
  try { localStorage.setItem("ava-mode", _mode); } catch (e) { /* ignore */ }
}

function setState(s) {
  state = s;
  orb.className = "orb";
  if (s === "listening") {
    orb.classList.add("listening");
    orbLabel.textContent = "listening...";
    statusText.textContent = "listening";
    statusDot.style.background = "var(--state-listening)";
  } else if (s === "thinking") {
    orb.classList.add("thinking");
    orbLabel.textContent = "thinking...";
    statusText.textContent = "thinking";
    statusDot.style.background = "var(--state-thinking)";
  } else if (s === "speaking") {
    orb.classList.add("speaking");
    orbLabel.textContent = "speaking";
    statusText.textContent = "speaking";
    statusDot.style.background = "var(--state-speaking)";
  } else if (s === "tap-to-unlock") {
    orb.classList.add("tap-to-unlock");
    orbLabel.textContent = "tap anywhere to resume audio";
    statusText.textContent = "tap to unlock";
    statusDot.style.background = "#ffcc00";
    statusDot.style.animation = "pulse 0.6s ease-in-out infinite";
  } else if (s === "error") {
    orb.classList.add("error");
    orbLabel.textContent = "error — see log";
    statusText.textContent = "error";
    statusDot.style.background = "#ff5050";
  } else {
    orbLabel.textContent = "tap to talk";
    statusText.textContent = "idle";
    statusDot.style.background = "var(--state-idle)";
    statusDot.style.animation = "none";
  }
}

function addTurn(role, text) {
  const empty = transcript.querySelector(".empty");
  if (empty) empty.remove();
  const turn = document.createElement("div");
  turn.className = "turn " + role;
  const label = role === "user" ? "you" : (role === "sam" ? "sam" : role);
  const replyId = "reply-" + Math.random().toString(36).slice(2, 10);
  if (role === "sam") {
    turn.innerHTML = `<div class="role">${label}</div><div class="text" id="${replyId}"></div><button class="play-btn" data-target="${replyId}" style="margin-top:6px;background:#374151;color:#a7f3d0;border:none;padding:4px 10px;border-radius:6px;font-size:12px;display:none;">▶ replay</button>`;
  } else {
    turn.innerHTML = `<div class="role">${label}</div><div class="text"></div>`;
  }
  turn.querySelector(".text").textContent = text;
  transcript.appendChild(turn);
  transcript.scrollTop = transcript.scrollHeight;
  // wire replay button
  if (role === "sam") {
    const btn = turn.querySelector(".play-btn");
    if (btn) {
      btn.onclick = () => {
        const url = btn.dataset.audioUrl;
        if (url) playAudio(url, btn);
        else dbg("replay: no audio_url stored on this turn");
      };
    }
  }
}

// --- debug logger + audio helper ---
// iOS Safari blocks Audio.play() if it isn't called inside (or chained to) a
// user gesture. A fetch that takes 5+ seconds breaks the gesture chain, so the
// first chat/listen call after page load hits "NotAllowedError" silently.
//
// Reliable unlock strategy: create an AudioContext lazily on first user
// gesture, then decode every reply sentence into an AudioBuffer and play via
// AudioBufferSourceNode. Once the AudioContext is in "running" state, plays
// from its source nodes work without further gestures — even across WS
// round-trips and async boundaries. This is more reliable than the silent-WAV
// HTMLMediaElement unlock, which only works for plays initiated within the
// same JS task as the original gesture.
let _audio = null;          // legacy <audio> element (kept for /playback URLs)
let _audioCtx = null;       // shared AudioContext (unlocked on first gesture)
let _audioCtxReady = false; // AudioContext is in "running" state
function _ensureAudioCtx() {
  if (!_audioCtx) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) { dbg("audio: no AudioContext available"); return null; }
    _audioCtx = new Ctx({ latencyHint: "interactive" });
  }
  return _audioCtx;
}
function _unlockAudio() {
  if (_audioCtxReady) return;
  const ctx = _ensureAudioCtx();
  if (!ctx) return;
  // resume() must be called inside (or chained to) a user gesture handler.
  // If the context is already running, this is a no-op.
  if (ctx.state === "suspended") {
    ctx.resume().then(() => {
      _audioCtxReady = (ctx.state === "running");
      dbg("audio: AudioContext resumed → " + ctx.state);
    }).catch(e => dbg("audio: resume failed " + e.name));
  } else {
    _audioCtxReady = (ctx.state === "running");
    dbg("audio: AudioContext state=" + ctx.state);
  }
  // Also keep the legacy <audio> element primed so /playback URLs work.
  if (!_audio) {
    _audio = new Audio();
    _audio.preload = "auto";
  }
}
// Backwards-compat: legacy callers (playAudio, audio-test chime) want a
// shared <audio> element. Return the singleton.
function _getAudio() {
  if (!_audio) {
    _audio = new Audio();
    _audio.preload = "auto";
  }
  return _audio;
}
let _dbgLog = [];
function dbg(msg) {
  const ts = new Date().toISOString().slice(11, 23);
  const line = "[" + ts + "] " + msg;
  _dbgLog.push(line);
  if (_dbgLog.length > 200) _dbgLog = _dbgLog.slice(-200);
  console.log(line);
  const pre = document.getElementById("debug-log");
  if (pre) { pre.textContent = _dbgLog.join("\\n"); pre.scrollTop = pre.scrollHeight; }
  // also POST to server-side log for long-term debug
  fetch("/api/debug", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ts: ts, msg: msg, ua: navigator.userAgent }) }).catch(() => {});
}
function playAudio(url, btn) {
  dbg("playAudio: " + url);
  setState("speaking");
  // Reuse the same <audio> element so iOS doesn't keep allocating new players
  const a = _getAudio();
  a.src = url;
  a.onended = () => { dbg("playAudio: ended"); setState("idle"); };
  a.onerror = (e) => { dbg("playAudio: ERROR " + (a.error ? a.error.code : "?") + " — " + (a.error ? a.error.message : "unknown")); setState("idle"); };
  a.oncanplay = () => dbg("playAudio: canplay (duration=" + a.duration.toFixed(2) + "s)");
  const p = a.play();
  if (p && p.catch) {
    p.then(() => dbg("playAudio: promise resolved"))
     .catch(e => { dbg("playAudio: PROMISE REJECTED " + e.name + ": " + e.message); setState("idle"); });
  }
}
document.addEventListener("DOMContentLoaded", () => {
  const dbgBtn = document.getElementById("debug-btn");
  const dbgClose = document.getElementById("debug-close");
  const dbgPanel = document.getElementById("debug-panel");
  const audioTestBtn = document.getElementById("audio-test-btn");
  if (dbgBtn) dbgBtn.onclick = () => { dbgPanel.style.display = "block"; dbg("debug panel opened"); };
  if (dbgClose) dbgClose.onclick = () => { dbgPanel.style.display = "none"; };
  if (audioTestBtn) audioTestBtn.onclick = () => {
    _unlockAudio();
    // 440Hz tone, 0.4s, generated as a data: WAV so no network needed.
    const sr = 22050, dur = 0.4, freq = 440;
    const n = Math.floor(sr * dur);
    const buf = new ArrayBuffer(44 + n * 2);
    const v = new DataView(buf);
    const ws = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o+i, s.charCodeAt(i)); };
    ws(0, "RIFF"); v.setUint32(4, 36+n*2, true); ws(8, "WAVE");
    ws(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
    v.setUint16(22, 1, true); v.setUint32(24, sr, true); v.setUint32(28, sr*2, true);
    v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    ws(36, "data"); v.setUint32(40, n*2, true);
    for (let i = 0; i < n; i++) {
      // 5ms attack/release envelope to avoid clicks
      let env = 1;
      if (i < sr*0.01) env = i/(sr*0.01);
      else if (i > n - sr*0.05) env = (n-i)/(sr*0.05);
      v.setInt16(44 + i*2, Math.sin(2*Math.PI*freq*i/sr) * env * 16000, true);
    }
    // Convert to base64 data URL
    const bytes = new Uint8Array(buf);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    const dataUrl = "data:audio/wav;base64," + btoa(bin);
    const a = _getAudio();
    a.src = dataUrl;
    a.play().then(() => dbg("audioTest: played 440Hz chime"))
             .catch(e => dbg("audioTest: FAILED " + e.name + ": " + e.message));
  };
  dbg("page loaded; ua=" + navigator.userAgent.slice(0, 80));
});

async function startMic() {
  if (isRecording) {
    stopMic();
    return;
  }
  // Detect whether this browser supports mic access
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    showHint("This browser doesn't support microphone access. Open this page in Safari or Chrome to use voice. Text input still works below.");
    return;
  }
  // Some embedded browsers (Telegram, in-app webviews) lack getUserMedia entirely.
  if (window.location.protocol !== "https:" && window.location.hostname !== "127.0.0.1" && window.location.hostname !== "localhost" && window.location.hostname !== "127.0.0.1") {
    showHint("Mic requires HTTPS or localhost. You're on " + window.location.protocol + "//" + window.location.hostname);
    return;
  }
  try {
    // v0.8: AudioContext with latencyHint 'interactive' shaves capture buffer
    // latency on iOS Safari from ~250ms (default 'interactive') to ~80ms.
    const audioCtx = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: 16000,
      latencyHint: "interactive",
    });
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, sampleRate: 16000, echoCancellation: true, noiseSuppression: true }
    });

    // Try WebSocket push-to-talk path first (v0.8). Falls back to HTTP /api/listen
    // if WS is unavailable.
    if (typeof WebSocket !== "undefined") {
      try {
        await startMicWS(stream);
        return;
      } catch (e) {
        dbg("ws push-to-talk unavailable, using legacy /api/listen: " + e.message);
      }
    }

    // Legacy fallback: MediaRecorder → webm blob → /api/listen
    mediaRecorder = new MediaRecorder(stream, { mimeType: "audio/webm" });
    audioChunks = [];
    mediaRecorder.ondataavailable = e => audioChunks.push(e.data);
    mediaRecorder.onstop = async () => {
      const blob = new Blob(audioChunks, { type: "audio/webm" });
      // Convert webm to wav via a quick decode
      const ctx = new AudioContext({ sampleRate: 16000 });
      const arrayBuf = await blob.arrayBuffer();
      const decoded = await ctx.decodeAudioData(arrayBuf);
      const pcm = decoded.getChannelData(0);
      const wav = encodeWav(pcm, 16000);
      await sendAudio(wav);
      stream.getTracks().forEach(t => t.stop());
    };
    mediaRecorder.start();
    isRecording = true;
    setState("listening");
  } catch (e) {
    showHint("Mic access failed: " + e.message + ". You can type messages below.");
  }
}

// ---- v0.8 WebSocket push-to-talk ----
// Stream MediaRecorder webm chunks over a single WebSocket to /ws. On stop,
// the server runs STT, then streams the brain reply sentence-by-sentence
// with Piper audio chunks interleaved. Audio playback is queued so we hear
// sentence 1 while sentence 2 is still being synthesized.
//
// v0.9 playback path: each WAV is decoded via AudioContext.decodeAudioData
// and played via AudioBufferSourceNode. This survives the WS/async boundary
// without needing a fresh user gesture on iOS Safari, as long as the
// AudioContext was resumed during the original PTT press.
let _ws = null;
let _wsSentences = [];     // queue of base64 WAVs waiting to play
// v0.10.1 — how many audio frames we actually received for THIS turn.
// Used to decide whether the /api/audio fallback is needed. If the WS
// audio arrived, playing the fallback as well made the user hear the
// whole reply twice.
let _wsAudioFramesReceived = 0;
let _wsPlaying = false;    // are we currently playing back-to-back?
// ── v0.10: shared WS message handler ───────────────────────────
//
// Used by BOTH the push-to-talk path and the duplex path. Keeping it
// as one named function means the two modes behave identically on the
// wire, and a fix to one fixes the other.
function _wsMessageHandler(e) {
  let msg;
  try { msg = JSON.parse(e.data); } catch { return; }
  const t = msg.type;
  if (t === "state") {
    dbg("ws state: " + msg.state);
    // v0.10.1 — a new turn starts when the server goes to "thinking".
    // Reset the audio-frame counter so the /api/audio fallback decision
    // is per-turn, not per-session. Without this, turn 2 would inherit
    // turn 1's count and skip its fallback even if its WS audio was
    // dropped.
    if (msg.state === "thinking") {
      // v0.10.8 — only reset the counter at the START of a turn, and
      // never in duplex.
      //
      // The counter answers "did this turn produce audio?", and the
      // 'done' message uses it to decide whether the /api/audio
      // fallback is needed. In duplex the server announces "thinking"
      // for turn N+1 while turn N's audio is still playing. Resetting
      // here wiped the count for the turn that was still in flight,
      // so its 'done' saw zero frames and fired the fallback on top
      // of the audio that was already playing -- two voices at once.
      //
      // In duplex the counter is reset when the reply actually starts
      // (the first 'text' delta for that turn), which is the only
      // point that is unambiguously "a new reply is beginning".
      if (_mode !== "duplex") {
        _wsAudioFramesReceived = 0;
      }
    }
    setState(msg.state);
  } else if (t === "transcript") {
    // STT result landed — show it as a "you" turn
    if (msg.final) {
      addTurn("user", msg.final);
      dbg("stt: " + msg.stt_ms + "ms → " + JSON.stringify(msg.final));
    }
  } else if (t === "text") {
    // Stream the reply text into the live DOM element so it appears
    // word-by-word as the brain produces it.
    if (!_wsCurrentReplyEl) {
      // v0.10.8 — the first text delta of a reply is the unambiguous
      // start of a new turn. In duplex this is where the per-turn
      // audio counter is reset, because the server's "thinking"
      // announcement arrives while the previous turn may still be
      // playing and is therefore not a safe reset point.
      _wsAudioFramesReceived = 0;
      const empty = transcript.querySelector(".empty");
      if (empty) empty.remove();
      const turn = document.createElement("div");
      turn.className = "turn sam";
      turn.innerHTML = `<div class="role">sam</div><div class="text"></div>`;
      transcript.appendChild(turn);
      _wsCurrentReplyEl = turn.querySelector(".text");
    }
    _wsCurrentReplyEl.textContent += msg.text;
    transcript.scrollTop = transcript.scrollHeight;
    if (!_textDbgFired) { dbg("ws text: streaming reply, " + (msg.text || "").length + " chars"); _textDbgFired = true; }
  } else if (t === "audio") {
    // Queue sentence audio. _wsPlayQueue() picks the next one and chains.
    dbg("ws audio msg: b64=" + (msg.data_b64 ? msg.data_b64.length : 0) + " provider=" + msg.provider + " sr=" + msg.sample_rate);
    _wsAudioFramesReceived++;
    _wsSentences.push(msg.data_b64);
    _wsPlayQueue();
  } else if (t === "pending_confirm") {
    addTurn("sam", "⚠ " + msg.summary);
    setState("idle");
  } else if (t === "barge_in") {
    // v0.10 — the user interrupted. Stop any queued/playing audio NOW
    // so Sam stops talking immediately, and show what was cut off.
    dbg("ws barge_in: phase=" + msg.phase + " cut=" +
        (msg.reply_text ? msg.reply_text.length : 0) + " chars");
    // v0.10.8 — stop every audio source, not just the WebAudio one.
    // The legacy <audio> fallback and the streamed sentence audio are
    // separate systems; stopping only one of them is how two voices
    // ended up talking over each other.
    _wsStopAllAudio();
    // v0.10.3 — hard-stop the audio context. Sam's voice bleeding into
    // the phone's mic is what makes the captured utterance dirty: the
    // server hears Sam talking over you and can't separate the two.
    // Suspending the context kills the playback graph completely.
    try {
      if (_bargeCtx && _bargeCtx.state === "running") {
        // Do NOT suspend here — the PCM tap lives on this context and
        // suspending it would kill the capture. Instead, just make sure
        // no source is connected to the destination.
      }
    } catch (e) { /* ignore */ }
    if (_wsCurrentReplyEl) {
      _wsCurrentReplyEl.classList.add("interrupted");
      _wsCurrentReplyEl = null;
    }
    // Flash the orb to show the interrupt landed.
    orb.classList.add("interrupted");
    setTimeout(() => orb.classList.remove("interrupted"), 600);
    if (_mode === "duplex") {
      // Stay live — we're mid-conversation, the mic never turned off.
      _bargeEnabled = true;
      _bargeBuf = [];
      // v0.10.3 — reset the end-of-speech detector so the interrupt
      // speech itself is captured as a fresh utterance, not merged with
      // the previous one.
      _duplexSpeaking = false;
      _duplexSpeechMs = 0;
      _duplexLastVoice = performance.now();
      setState("listening");
    } else {
      _bargeEnabled = false;
      _bargeBuf = [];
      setState("listening");
    }
  } else if (t === "done") {
    dbg("ws done: " + msg.reply_len + " chars in " + msg.total_ms + "ms");
    _wsCurrentReplyEl = null;
    _textDbgFired = false;
    // v0.10 — turn the PCM tap off; the turn is over.
    // EXCEPT in duplex, where the mic must stay hot for the next
    // utterance. Setting _bargeEnabled = false here killed the PCM
    // tap at the end of every reply, so after Sam spoke once the mic
    // was dead and the user could never be heard again. That is
    // exactly the "duplex once, then deaf" symptom.
    if (_mode === "duplex") {
      // Stay live and re-arm the end-of-speech detector.
      _bargeEnabled = true;
      _bargeBuf = [];
      _duplexSpeaking = false;
      _duplexSpeechMs = 0;
      _duplexLastVoice = performance.now();
      setState("listening");
    } else {
      _bargeEnabled = false;
      _bargeBuf = [];
    }
    // v0.9.3 fallback: server sends a single /api/audio URL in `done` so
    // the iOS Safari <audio> element can play it directly. WS-streamed
    // sentence audio was getting silently dropped on iPhone.
    //
    // v0.10.1 — ONLY use the fallback if the WS audio path did not
    // actually play. The old code played it unconditionally, which
    // meant: the sentence-by-sentence audio played from the WS stream,
    // and then the fallback replayed the ENTIRE reply from the start.
    // The user heard the reply twice — the "it kills your initial reply
    // and has to repeat itself" symptom.
    //
    // The fallback exists for the case where the WS frames were dropped
    // on the phone. If we actually received and played audio frames, we
    // do NOT need it.
    const _wsGotAudio = _wsAudioFramesReceived > 0;
    if (msg.audio_url) {
      dbg("ws done: fallback audio_url=" + msg.audio_url + " size=" + msg.audio_size +
          " (ws audio frames received=" + _wsAudioFramesReceived + ")");
      if (_wsGotAudio) {
        // The WS audio path worked. Do NOT replay the whole reply.
        dbg("ws done: ws audio already played — skipping fallback");
      } else {
        // No WS audio arrived — the phone dropped the frames. Use the
        // fallback so the user still hears the reply.
        dbg("ws done: no ws audio frames — using fallback");
        // v0.10.8 — stop EVERY source before starting the fallback.
        // The legacy <audio> element may still be playing a previous
        // turn's fallback, and WebAudio may have a queued sentence.
        // Starting the fallback on top of either is exactly the
        // "two voices at once" symptom.
        _wsStopAllAudio();
        // Use legacy <audio> path — same code as /api/listen reply handler
        const a = _getAudio();
        a.src = msg.audio_url;
        a.play().then(() => {
          setState("speaking");
          dbg("ws done: fallback audio playing");
          if (_ws && _ws.readyState === 1) {
            _ws.send(JSON.stringify({ type: "playing", value: true }));
          }
        }).catch(e => {
          dbg("ws done: fallback audio play failed " + e.name);
          setState("idle");
        });
        a.onended = () => {
          dbg("ws done: fallback audio ended");
          if (_ws && _ws.readyState === 1) {
            _ws.send(JSON.stringify({ type: "playing", value: false }));
          }
          setState("idle");
        };
      }
    }
  } else if (t === "pong" || t === "keepalive") {
    // keepalive
  } else {
    dbg("ws unhandled msg type=" + t);
  }
}

let _wsCurrentReplyEl = null; // live reply DOM element being appended-to
let _wsActiveSource = null; // currently-playing AudioBufferSourceNode
let _textDbgFired = false;  // one-shot dbg so we know text deltas reached the client

// Decode a base64 WAV into an AudioBuffer via the shared AudioContext.
async function _wsDecodeSentence(wav_b64) {
  const ctx = _ensureAudioCtx();
  if (!ctx) throw new Error("no AudioContext");
  // base64 → ArrayBuffer
  const bin = atob(wav_b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  // decodeAudioData expects ArrayBuffer; works for the raw PCM WAV Piper emits.
  return await ctx.decodeAudioData(bytes.buffer);
}

// v0.10.8 — stop EVERY audio source at once.
//
// The bridge drives two independent audio systems: the WebAudio
// AudioBufferSourceNode (streamed sentence audio) and the legacy
// <audio> element (the /api/audio fallback). They were stopped
// independently, so an interrupt could silence one while the other
// kept playing -- which is what produced the "two of you talking over
// each other" symptom. Every stop path now goes through here.
//
// Also cancels the in-flight decode. _wsPlayQueue() calls
// _wsDecodeSentence() and only THEN plays; clearing _wsSentences
// does not cancel a decode that is already awaiting its promise, so
// the sentence played anyway after the interrupt.
function _wsStopAllAudio() {
  // Cancel a decode that is in flight.
  _wsPlayToken = (_wsPlayToken || 0) + 1;

  // Stop the legacy <audio> element (the fallback player).
  try {
    const a = _getAudio();
    a.pause();
    a.removeAttribute("src");
    a.load();
  } catch (e) { /* ignore */ }

  // Stop the currently-playing WebAudio source.
  if (_wsActiveSource) {
    try { _wsActiveSource.stop(); } catch (e) {}
    try { _wsActiveSource.disconnect(); } catch (e) {}
    _wsActiveSource = null;
  }

  // Drop anything queued.
  _wsSentences = [];
  _wsPlaying = false;
}

// Incremented on every stop so an in-flight decode can tell it has
// been cancelled.
let _wsPlayToken = 0;

function _wsPlayOne(buffer) {
  const ctx = _ensureAudioCtx();
  if (!ctx) return;
  const src = ctx.createBufferSource();
  src.buffer = buffer;
  src.connect(ctx.destination);
  _wsActiveSource = src;
  src.onended = () => {
    _wsActiveSource = null;
    _wsPlaying = false;
    // v0.10.4 — and tell the server it stopped.
    if (_ws && _ws.readyState === 1) {
      _ws.send(JSON.stringify({ type: "playing", value: false }));
    }
    if (_wsSentences.length > 0) {
      _wsPlayQueue();
    } else {
      setState("idle");
    }
  };
  _wsPlaying = true;
  setState("speaking");
  // v0.10.4 — tell the server audio is actually PLAYING now. The
  // server's VAD uses this to pick the bleed-safe playback threshold.
  // Previously it set the flag when the frame was SENT, which is many
  // seconds before the audio is audible — so the detector was using the
  // wrong threshold for most of the reply.
  if (_ws && _ws.readyState === 1) {
    _ws.send(JSON.stringify({ type: "playing", value: true }));
  }
  // Start immediately. The AudioContext was resumed during the PTT press,
  // so this works inside any async callback.
  src.start();
}

function _wsPlayQueue() {
  if (_wsPlaying || _wsSentences.length === 0) return;
  // If the AudioContext hasn't been unlocked yet (page loaded but no PTT
  // press yet), fall back to the legacy gesture-required path.
  if (!_audioCtxReady || !_audioCtx || _audioCtx.state !== "running") {
    dbg("audio: queue waiting for gesture (ctx state=" + (_audioCtx ? _audioCtx.state : "none") + ")");
    _wsWaitingForGesture = true;
    setState("tap-to-unlock");
    return;
  }
  const wav_b64 = _wsSentences.shift();
  // Capture the current cancel token. If _wsStopAllAudio() runs while
  // the decode is awaiting, the token changes and the decoded buffer
  // is dropped instead of played.
  const token = _wsPlayToken;
  _wsDecodeSentence(wav_b64)
    .then(buf => {
      if (token !== _wsPlayToken) {
        dbg("audio: decode cancelled (interrupt)");
        return;
      }
      _wsPlayOne(buf);
    })
    .catch(e => {
      if (token !== _wsPlayToken) return;   // cancelled -- do not requeue
      dbg("audio: decode failed " + e.name + " — requeuing, need tap-to-unlock");
      _wsSentences.unshift(wav_b64);
      _wsWaitingForGesture = true;
      setState("tap-to-unlock");
    });
}

// If we hit an audio-context unlock failure (e.g. decode error on a malformed
// sentence), the next pointerdown or touchstart anywhere on the page is a
// valid user gesture to retry.
let _wsWaitingForGesture = false;
function _wsGestureUnlock() {
  if (!_wsWaitingForGesture) return;
  _wsWaitingForGesture = false;
  // Re-resume the AudioContext inside the gesture, then drain the queue.
  _unlockAudio();
  // Small delay so ctx.state has a chance to transition to "running" before
  // we kick the queue. resume() is async but resolves within a tick on iOS.
  setTimeout(() => {
    if (_audioCtx && _audioCtx.state === "running") {
      _audioCtxReady = true;
      dbg("audio: gesture unlock ok (ctx running)");
      _wsPlayQueue();
    } else {
      // Fallback: legacy HTMLAudioElement path. Should be rare with v0.9.
      const a = _getAudio();
      try {
        a.src = "";
        const p = a.play();
        if (p && p.then) p.then(() => { a.pause(); a.currentTime = 0; dbg("audio: legacy unlock ok"); _wsPlayQueue(); }).catch(() => { _wsPlayQueue(); });
        else _wsPlayQueue();
      } catch (e) { _wsPlayQueue(); }
    }
  }, 30);
}
document.addEventListener("pointerdown", _wsGestureUnlock, { passive: true });
document.addEventListener("touchstart", _wsGestureUnlock, { passive: true });

function _wsReset() {
  // v0.10.8 — stop every audio source, including the legacy <audio>
  // fallback element. _wsReset runs on mode changes and reconnects;
  // if the fallback was mid-playback it used to keep going and play
  // over whatever came next.
  _wsStopAllAudio();
  _wsCurrentReplyEl = null;
  _textDbgFired = false;
  _wsAudioFramesReceived = 0;   // v0.10.1 — new turn, new audio count
  setState("idle");
}

async function startMicWS(stream) {
  // Decide on a mimeType the browser actually supports. iOS Safari often
  // refuses audio/webm; fallback to audio/mp4.
  const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
    ? "audio/webm;codecs=opus"
    : (MediaRecorder.isTypeSupported("audio/mp4") ? "audio/mp4" : "audio/webm");

  mediaRecorder = new MediaRecorder(stream, { mimeType: mime });
  audioChunks = [];

  // Open the WS
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  _ws = new WebSocket(proto + "//" + window.location.host + "/ws");
  _wsSentences = [];
  _wsPlaying = false;
  _wsCurrentReplyEl = null;
  _textDbgFired = false;
  _wsAudioFramesReceived = 0;

  await new Promise((resolve, reject) => {
    _ws.onopen = () => {
      _ws.send(JSON.stringify({ type: "start", session_id: SESSION_ID }));
      // v0.10 — attach the raw-PCM tap so the server can run a VAD on
      // the mic while Sam is speaking. Harmless if barge-in is disabled
      // server-side: the server just ignores the frames.
      _attachPcmTap(stream);
      resolve();
    };
    _ws.onerror = e => { reject(new Error("ws connect failed")); };
    setTimeout(() => reject(new Error("ws timeout")), 3000);
  });

  // Stream chunks as MediaRecorder emits them. timeslice=100 → ~100ms chunks.
  mediaRecorder.ondataavailable = e => {
    if (e.data.size > 0) {
      audioChunks.push(e.data);
      // Send this chunk over WS as base64. We could send binary directly,
      // but JSON keeps the protocol uniform with the rest of /ws.
      const reader = new FileReader();
      reader.onload = () => {
        const b64 = reader.result.split(",")[1];
        if (_ws && _ws.readyState === 1) {
          _ws.send(JSON.stringify({
            type: "audio",
            mime: mime,
            data_b64: b64,
          }));
        }
      };
      reader.readAsDataURL(e.data);
    }
  };

  mediaRecorder.onstop = () => {
    if (_ws && _ws.readyState === 1) {
      _ws.send(JSON.stringify({ type: "stop" }));
      // v0.10 — start streaming raw PCM to the server for the whole
      // reply, so the user can interrupt Sam mid-sentence.
      _bargeEnabled = true;
      dbg("barge: pcm streaming on");
    }
    // NOTE: the mic tracks are intentionally NOT stopped here. The
    // MediaRecorder and the PCM tap share the same MediaStream, and the
    // tap must keep running for the whole reply so the user can
    // interrupt. We stop the tracks in _detachPcmTap() when the turn
    // ends (on "done" / "barge_in" / ws close).
  };

  // WS message handler
  _ws.onmessage = _wsMessageHandler;

  _ws.onclose = () => {
    dbg("ws closed");
    _ws = null;
    if (state === "listening" || state === "thinking") _wsReset();
  };

  mediaRecorder.start(100);  // 100ms timeslice = ~10 chunks/sec streaming
  isRecording = true;
  setState("listening");
}

let _hintTimeout = null;
function showHint(msg) {
  let hint = document.getElementById("hint");
  if (!hint) {
    hint = document.createElement("div");
    hint.id = "hint";
    hint.style.cssText = "position:fixed;bottom:80px;left:20px;right:20px;background:#1e3a8a;color:#fff;padding:14px 18px;border-radius:12px;font-size:14px;line-height:1.4;z-index:1000;box-shadow:0 4px 20px rgba(0,0,0,0.4);";
    document.body.appendChild(hint);
  }
  hint.textContent = msg;
  hint.style.display = "block";
  if (_hintTimeout) clearTimeout(_hintTimeout);
  _hintTimeout = setTimeout(() => { hint.style.display = "none"; }, 8000);
}

function stopMic() {
  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    mediaRecorder.stop();
  }
  isRecording = false;
}

function encodeWav(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeStr = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off+i, s.charCodeAt(i)); };
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, "data");
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}

async function sendAudio(wavBlob) {
  setState("thinking");
  const fd = new FormData();
  fd.append("audio", wavBlob, "input.wav");
  fd.append("session_id", SESSION_ID);
  dbg("sendAudio: " + wavBlob.size + " bytes");
  try {
    const r = await fetch("/api/listen", { method: "POST", body: fd });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    dbg("listen reply: user=" + JSON.stringify(data.user_text) + " sam=" + JSON.stringify(data.reply_text) + " audio=" + (data.audio_url || "NONE") + " size=" + (data.audio_size || 0));
    if (data.user_text) addTurn("user", data.user_text);
    if (data.reply_text) addTurn("sam", data.reply_text);
    if (data.audio_url) {
      const btn = transcript.querySelector(".turn:last-child .play-btn");
      if (btn) { btn.dataset.audioUrl = data.audio_url; btn.style.display = "inline-block"; }
      playAudio(data.audio_url, btn);
    } else {
      dbg("listen: no audio_url returned — server-side synth returned empty");
      setState("idle");
    }
  } catch (e) {
    dbg("sendAudio FAILED: " + e.message);
    showHint("send failed: " + e.message);
    setState("idle");
  }
}

async function sendText() {
  const text = textInput.value.trim();
  if (!text) return;
  textInput.value = "";
  addTurn("user", text);
  setState("thinking");
  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, session_id: SESSION_ID }),
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    if (data.reply_text) addTurn("sam", data.reply_text);
    renderPending(data.pending_confirm);
    renderToolLog(data.tool_log || []);
    if (data.audio_url) {
      const btn = transcript.querySelector(".turn:last-child .play-btn");
      if (btn) { btn.dataset.audioUrl = data.audio_url; btn.style.display = "inline-block"; }
      playAudio(data.audio_url, btn);
    } else {
      dbg("chat: no audio_url returned");
      setState("idle");
    }
  } catch (e) {
    alert("send failed: " + e.message);
    setState("idle");
  }
}

function renderPending(pending) {
  let el = document.getElementById("pending-banner");
  if (!el) {
    el = document.createElement("div");
    el.id = "pending-banner";
    el.style.cssText = "margin:10px 0;padding:10px 14px;border-radius:8px;background:#7c2d12;color:#fed7aa;font-size:13px;display:none;";
    transcript.parentNode.insertBefore(el, transcript.nextSibling);
  }
  if (!pending) { el.style.display = "none"; el.textContent = ""; return; }
  el.style.display = "block";
  el.innerHTML = `<strong>⚠ Held action:</strong> ${pending.summary}. <em>Say "do it" or "cancel".</em>`;
}

function renderToolLog(entries) {
  let el = document.getElementById("tool-log");
  if (!el) {
    el = document.createElement("div");
    el.id = "tool-log";
    el.style.cssText = "margin:10px 0;padding:8px 12px;border-radius:8px;background:#0f172a;color:#94a3b8;font-size:11px;font-family:monospace;";
    transcript.parentNode.insertBefore(el, transcript.nextSibling);
  }
  if (!entries.length) { el.style.display = "none"; el.textContent = ""; return; }
  el.style.display = "block";
  el.innerHTML = "<strong style='color:#cbd5e1'>tool calls:</strong><br>" +
    entries.map(e => {
      const color = e.tier === 'external_write' ? '#fb923c' : (e.tier === 'local_write' ? '#a3e635' : '#60a5fa');
      return `<span style="color:${color}">[${e.tier}]</span> ${e.tool}(${JSON.stringify(e.args).slice(0,60)}) → ${e.result_summary}`;
    }).join("<br>");
}

orb.addEventListener("click", () => {
  _unlockAudio();
  if (_mode === "duplex") return;  // orb is a status indicator in duplex
  startMic();
});

// v0.10 — mode toggle (push-to-talk ↔ duplex)
modePttBtn.addEventListener("click", () => { _unlockAudio(); _setMode("ptt"); });
modeDuplexBtn.addEventListener("click", () => { _unlockAudio(); _setMode("duplex"); });

// v0.10.5 — mute button
const muteBtn = document.getElementById("mute-btn");
if (muteBtn) {
  muteBtn.addEventListener("click", () => {
    _unlockAudio();
    _setMute(!_muted);
  });
}

// Restore the last-used mode. Duplex needs a user gesture for the mic,
// so we only restore the BUTTON state; the mic starts on first tap.
try {
  const savedMode = localStorage.getItem("ava-mode");
  if (savedMode === "duplex") {
    modePttBtn.classList.remove("active");
    modeDuplexBtn.classList.add("active");
    _mode = "duplex";
    stageEl.classList.add("duplex");
    // v0.10.5 — the mute button is a duplex-only control, so the body
    // class has to be set here too (this path bypasses _setMode).
    document.body.classList.add("duplex-mode");
    orbLabel.textContent = "tap to start duplex";
    duplexHint.innerHTML = "Tap the orb to start — mic needs a tap on iOS.";
  }
} catch (e) { /* ignore */ }
sendBtn.addEventListener("click", () => { _unlockAudio(); sendText(); });
textInput.addEventListener("keydown", e => { if (e.key === "Enter") { _unlockAudio(); sendText(); } });
clearBtn.addEventListener("click", async () => {
  await fetch("/api/clear", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: SESSION_ID }),
  });
  transcript.innerHTML = '<div class="empty">conversation cleared</div>';
});

setState("idle");
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    # Stamp the build version and the channel into the header so it is
    # always obvious which instance you are talking to. "prod" renders
    # as plain text; "dev" is highlighted, because the dev instance is
    # the experimental one and should never be mistaken for production.
    if APP_CHANNEL == "dev":
        channel_html = (
            '<span style="color:#f5a623;font-weight:700;letter-spacing:.5px;">'
            "DEV</span>"
        )
    else:
        channel_html = '<span style="color:#4a5a7a;">prod</span>'
    html = HTML.replace("__VERSION__", APP_VERSION).replace("__CHANNEL__", channel_html)
    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


def _whisper_reachable() -> bool:
    try:
        r = requests.get("http://127.0.0.1:8178/", timeout=2)
        return r.status_code < 500
    except Exception:
        return False


@app.post("/api/chat")
async def chat(payload: dict):
    """Text in, text + audio out. Returns JSON with reply_text and audio URL.

    Agent-aware: streams text through the brain's agent loop, runs tools,
    and surfaces any pending confirmation request for irreversible actions.
    """
    text = (payload.get("text") or "").strip()
    session_id = payload.get("session_id") or f"ava-{uuid.uuid4().hex[:8]}"
    if not text:
        raise HTTPException(400, "text is required")

    log.info("chat: session=%s text=%r", session_id, text[:80])

    # Resolve any pending confirmation request before treating this as a new turn
    pending = brain.get_pending_confirm(session_id)
    if pending:
        decision = brain._looks_like_confirmation(text)
        if decision == "yes":
            result = brain.resolve_pending(session_id, "yes")
            log.info("chat: confirmation YES for session=%s → %s", session_id, result)
            reply_text = _format_confirmation_result(pending, result)
        elif decision == "no":
            cancelled = brain.resolve_pending(session_id, "no")
            log.info("chat: confirmation NO for session=%s", session_id)
            reply_text = f"Cancelled. I won't {pending['summary']}."
        else:
            # Not a yes/no — but there's a pending action. Clear it; treat
            # the reply as a fresh turn.
            log.info("chat: pending hold dropped, treating as new turn (session=%s)", session_id)
            brain.resolve_pending(session_id, "no")  # discard
            reply_text = await _brain_speak(text, session_id)
    else:
        reply_text = await _brain_speak(text, session_id)

    # Persist turn
    brain.append_turn(session_id, text, reply_text)

    # Synthesize
    wav_bytes, tts_provider = b"", "none"
    if reply_text.strip():
        loop = asyncio.get_event_loop()
        try:
            wav_bytes, tts_provider = await loop.run_in_executor(
                None, _synth_with_fallback, reply_text
            )
            log.info("chat: tts=%s bytes=%d", tts_provider, len(wav_bytes))
        except Exception as e:
            log.exception("synth failed: %s", e)
            wav_bytes, tts_provider = b"", "none"

    # Cache + URL
    audio_url = None
    if wav_bytes:
        import uuid as _uuid
        audio_id = f"{int(time.time()*1000)}-{_uuid.uuid4().hex[:6]}.wav"
        _AUDIO_CACHE[audio_id] = wav_bytes
        if len(_AUDIO_CACHE) > 50:
            oldest = next(iter(_AUDIO_CACHE))
            _AUDIO_CACHE.pop(oldest, None)
        audio_url = f"/api/audio/{audio_id}"

    # Surface pending confirm + tool log so the UI can show them
    held = brain.get_pending_confirm(session_id)
    return JSONResponse({
        "session_id": session_id,
        "user_text": text,
        "reply_text": reply_text,
        "audio_url": audio_url,
        "audio_size": len(wav_bytes) if wav_bytes else 0,
        "pending_confirm": held,
        "tool_log": brain.get_tool_log(session_id)[-10:],
    })


# ---- streaming TTS infrastructure ----
# v0.8: instead of waiting for the brain's full reply before synthesizing,
# we consume brain.stream_reply live, split text on sentence boundaries, and
# fire Piper per-sentence so the first WAV chunk can ship back to the client
# while the brain is still composing later ones.

import re as _re

_SENT_END = _re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'])|(?<=[.!?])$")
_SENT_FLOOR = 12   # don't fire Piper on a sentence shorter than this (chars)
_SENT_CEIL = 180   # hard cap — Piper quality drops on very long inputs


def _split_sentences(buffer: str) -> tuple[list[str], str]:
    """Split *buffer* on sentence boundaries; keep the trailing partial.

    Returns (complete_sentences, remainder). We never split mid-sentence —
    Piper gets whole sentences, not fragments, so intonation stays clean.
    """
    parts = _SENT_END.split(buffer)
    # _SENT_END.split returns: [text_before, sep, sentence, sep, sentence, ...]
    # We want the trailing complete ones. Strategy: every split produces
    # either a sep (whitespace) or text. A "complete sentence" is text that
    # is followed by a sep. The very last element is whatever trailing
    # partial hasn't been split yet.
    sentences: list[str] = []
    remainder: str = ""
    # Re-join with explicit separator tracking
    # Simpler approach: split on the regex matches directly
    last_end = 0
    for m in _SENT_END.finditer(buffer):
        end = m.end()
        sent = buffer[last_end:end].strip()
        if sent:
            sentences.append(sent)
        last_end = end
    remainder = buffer[last_end:]
    return sentences, remainder


async def _stream_brain_to_sentences(
    user_text: str,
    session_id: str,
    on_sentence,
    barge_state=None,
) -> str:
    """Consume brain.stream_reply live. For each completed sentence, call
    on_sentence(sentence_str) — that callback fires Piper and ships audio.

    Returns the full concatenated reply text (also passed to brain.append_turn
    by the caller). Non-text events (tool_call, pending_confirm, done) are
    ignored here — the caller wires them as needed.

    v0.10 — barge_state: if provided, the stream aborts as soon as the
    user starts talking (barge-in). The partially-said text is still
    returned so the caller can record it. `barge_state.tripped` is set to
    the phase that triggered the abort.
    """
    history = brain.get_history(session_id)
    text_buf: list[str] = []
    pending: str = ""  # un-split tail
    t0 = time.time()
    loop = asyncio.get_event_loop()
    aborted = False

    def _consume():
        return brain.stream_reply(
            user_text, history=history, session_id=session_id,
        )

    # Run the blocking sync generator in a thread; iterate as it produces.
    # We bridge sync-iter → async by polling the generator in chunks so the
    # event loop can interleave Piper synth + websocket sends.
    import queue
    # v0.10.1 — UNBOUNDED queue. It used to be maxsize=64 with a
    # BLOCKING q.put() in the producer. A long answer streams far more
    # than 64 text chunks before the first sentence terminator arrives,
    # so the producer filled the queue and blocked forever while the
    # consumer waited for a sentence that could never come. The bridge
    # sat at 0% CPU with the reply truncated and no error logged.
    # Text chunks are small; an unbounded queue removes the deadlock.
    q: queue.Queue = queue.Queue()

    def _producer():
        try:
            for evt in _consume():
                q.put(("evt", evt))
        except Exception as e:
            q.put(("err", e))
        finally:
            q.put(("end", None))

    prod_task = loop.run_in_executor(None, _producer)

    last_ship = time.time()
    while True:
        # v0.10.1 — stall watchdog. If the brain is producing but no
        # sentence has shipped for a long time, something is wedged
        # (a bounded queue, a blocked producer). Log it loudly rather
        # than hanging silently — a silent hang is how bug 3 presented.
        if time.time() - last_ship > 30:
            log.warning(
                "stream_brain: STALLED — no sentence shipped in %.0fs "
                "(queue=%d, barge=%s)",
                time.time() - last_ship, q.qsize(),
                getattr(barge_state, "tripped", None),
            )
            last_ship = time.time()

        # v0.10 — barge-in check. If the user started talking, stop the
        # reply dead. Checked at the TOP of every poll so we bail within
        # ~10ms of the trip, and again before each sentence ships so we
        # don't speak a sentence the user already interrupted.
        if barge_state is not None and barge_state.tripped and not aborted:
            aborted = True
            log.info(
                "ws barge-in: aborting brain stream (phase=%s)",
                barge_state.tripped,
            )
            break

        # Non-blocking poll so the event loop stays responsive
        try:
            item = q.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.01)
            continue

        kind, payload = item
        if kind == "err":
            log.exception("brain stream error")
            text_buf.append(f"\n\nI hit an error: {payload}\n")
            break
        if kind == "end":
            break

        evt = payload
        etype = evt.get("type")
        if etype == "text":
            delta = evt.get("delta", "")
            if not delta:
                continue
            pending += delta
            # Split off any complete sentences
            sentences, pending = _split_sentences(pending)
            for sent in sentences:
                # Skip too-short fragments (mid-word matches), but don't
                # starve the pipeline — force-flush if pending is huge.
                if len(sent) < _SENT_FLOOR and len(pending) < _SENT_CEIL:
                    pending = sent + " " + pending
                    continue
                # v0.10 — don't start speaking a sentence if the user has
                # already interrupted.
                if barge_state is not None and barge_state.tripped:
                    if not aborted:
                        aborted = True
                        log.info(
                            "ws barge-in: cut TTS before sentence %r", sent[:40]
                        )
                    break
                text_buf.append(sent)
                last_ship = time.time()
                # Fire the sentence callback (Piper + ship audio)
                try:
                    await on_sentence(sent)
                except WebSocketDisconnect:
                    # The client is gone — stop the reply dead.
                    aborted = True
                    log.info("ws: client disconnected mid-reply — aborting")
                    break
                except Exception as e:
                    log.warning("on_sentence failed: %s", e)
            if aborted:
                break
        # tool_call / pending_confirm / done / tool_result are ignored here —
        # the WS handler wires them separately if it cares.

    # v0.10 — if we aborted, don't flush the trailing partial as a final
    # sentence: the user is talking, we shouldn't keep speaking.
    if pending.strip() and not aborted:
        sentences, _ = _split_sentences(pending + ".")
        # The above returns sentences inside `pending` even though we passed
        # the whole thing; we just want the original pending back.
        text_buf.append(pending.strip())

    full = " ".join(text_buf).strip()
    log.info(
        "stream_brain: %.2fs → %d chars (%d sentences)%s",
        time.time() - t0, len(full), full.count(".!?") + full.count("."),
        " [BARGE-ABORTED]" if aborted else "",
    )
    return full


async def _brain_speak(user_text: str, session_id: str) -> str:
    """Run the agent loop, collect text deltas, return concatenated reply.

    v0.8: still used by /api/chat and /api/listen for the legacy path.
    Streaming consumers should use _stream_brain_to_sentences.
    """
    history = brain.get_history(session_id)
    text_parts: list[str] = []
    t0 = time.time()
    loop = asyncio.get_event_loop()
    try:
        # stream_reply is a sync generator; run in a thread so the event loop
        # can still breathe while the API streams.
        def _consume():
            return list(brain.stream_reply(
                user_text, history=history, session_id=session_id,
            ))
        events = await loop.run_in_executor(None, _consume)
        for evt in events:
            if evt.get("type") == "text":
                text_parts.append(evt.get("delta", ""))
        reply_text = "".join(text_parts).strip()
        log.info("chat: brain %.2fs → %d chars (%d events)", time.time() - t0,
                 len(reply_text), len(events))
        return reply_text
    except Exception as e:
        log.exception("brain.stream_reply failed")
        # Short sentinel — long error strings poison session history.
        return "I hit a brain error. Try again in a moment."


def _format_confirmation_result(pending: dict, result: dict | None) -> str:
    """Build the spoken reply after a 'yes' confirmation."""
    if result is None:
        return "Sorry, the action dropped. Try again?"
    if result.get("cancelled"):
        return f"Cancelled."
    if "error" in result:
        return f"I tried but hit an error: {result['error']}"
    if pending["tool"] == "send_message" and result.get("ok"):
        mid = result.get("message_id", "?")
        return f"Sent. Message id {mid}."
    if pending["tool"] == "send_message":
        return f"I couldn't send it — check the bridge log."
    return "Done."


# Cache recent audio responses in memory for the /api/audio/... route
_AUDIO_CACHE: dict[str, bytes] = {}


# Server-side debug log: collects browser events POSTed from /api/debug so we
# can troubleshoot long-term audio dropouts on mobile.
_CLIENT_DEBUG_LOG: list[dict] = []


@app.post("/api/debug")
async def client_debug(payload: dict):
    """Receive debug events from the web UI; append to in-memory ring + log file."""
    msg = payload.get("msg", "")
    ts = payload.get("ts", "")
    ua = payload.get("ua", "")
    entry = {"ts": ts, "msg": msg, "ua": ua}
    _CLIENT_DEBUG_LOG.append(entry)
    if len(_CLIENT_DEBUG_LOG) > 500:
        del _CLIENT_DEBUG_LOG[: len(_CLIENT_DEBUG_LOG) - 500]
    log.info("client-debug: [%s] %s (ua=%s)", ts, msg, ua[:40])
    return {"ok": True, "logged": len(_CLIENT_DEBUG_LOG)}


@app.get("/api/debug")
async def get_client_debug():
    """Retrieve the last 100 client debug events. Useful when the user reports an issue."""
    return {"events": _CLIENT_DEBUG_LOG[-100:]}


@app.get("/api/audio/{audio_id}")
async def get_audio(audio_id: str):
    """Return a cached audio response (24kHz mono WAV)."""
    wav = _AUDIO_CACHE.get(audio_id)
    if not wav:
        raise HTTPException(404, "audio not found or expired")
    return Response(content=wav, media_type="audio/wav", headers={
        "Content-Disposition": f"inline; filename={audio_id}",
        "Cache-Control": "public, max-age=300",
    })


@app.post("/api/listen")
async def listen(
    audio: UploadFile = File(...),
    session_id: Optional[str] = Form(None),
):
    """Audio in (WAV/webm/m4a), text + audio out. Returns JSON like /api/chat."""
    sid = session_id or f"ava-{uuid.uuid4().hex[:8]}"
    log.info("listen: session=%s file=%s content_type=%s", sid, audio.filename, audio.content_type)

    audio_bytes = await audio.read()
    log.info("listen: got %d bytes", len(audio_bytes))

    # Step 1: STT
    t0 = time.time()
    try:
        user_text = await asyncio.get_event_loop().run_in_executor(
            None, whisper_stt.transcribe, audio_bytes
        )
    except Exception as e:
        log.exception("whisper_stt.transcribe failed")
        raise HTTPException(500, f"stt error: {e}")
    log.info("listen: stt %.2fs → %r", time.time() - t0, user_text)

    if not user_text:
        return JSONResponse({
            "session_id": sid,
            "user_text": "",
            "reply_text": "I didn't catch that. Could you try again?",
            "audio_url": None,
        })

    # Step 2: handle confirmation OR speak (mirrors /api/chat)
    pending = brain.get_pending_confirm(sid)
    if pending:
        decision = brain._looks_like_confirmation(user_text)
        if decision == "yes":
            result = brain.resolve_pending(sid, "yes")
            log.info("listen: confirmation YES for session=%s → %s", sid, result)
            reply_text = _format_confirmation_result(pending, result)
        elif decision == "no":
            brain.resolve_pending(sid, "no")
            log.info("listen: confirmation NO for session=%s", sid)
            reply_text = f"Cancelled. I won't {pending['summary']}."
        else:
            brain.resolve_pending(sid, "no")
            log.info("listen: pending hold dropped, treating as new turn (session=%s)", sid)
            reply_text = await _brain_speak(user_text, sid)
    else:
        reply_text = await _brain_speak(user_text, sid)

    log.info("listen: brain → %d chars", len(reply_text))
    brain.append_turn(sid, user_text, reply_text)

    # Step 3: TTS
    wav_bytes, tts_provider = b"", "none"
    if reply_text.strip():
        loop = asyncio.get_event_loop()
        try:
            wav_bytes, tts_provider = await loop.run_in_executor(
                None, _synth_with_fallback, reply_text
            )
            log.info("listen: tts=%s bytes=%d", tts_provider, len(wav_bytes))
        except Exception as e:
            log.exception("synth failed: %s", e)

    audio_url = None
    if wav_bytes:
        import uuid as _uuid
        audio_id = f"{int(time.time()*1000)}-{_uuid.uuid4().hex[:6]}.wav"
        _AUDIO_CACHE[audio_id] = wav_bytes
        if len(_AUDIO_CACHE) > 50:
            oldest = next(iter(_AUDIO_CACHE))
            _AUDIO_CACHE.pop(oldest, None)
        audio_url = f"/api/audio/{audio_id}"

    return JSONResponse({
        "session_id": sid,
        "user_text": user_text,
        "reply_text": reply_text,
        "audio_url": audio_url,
        "audio_size": len(wav_bytes) if wav_bytes else 0,
        "pending_confirm": brain.get_pending_confirm(sid),
        "tool_log": brain.get_tool_log(sid)[-10:],
    })


@app.post("/api/clear")
async def clear(payload: dict):
    session_id = payload.get("session_id")
    if session_id:
        brain.clear_session(session_id)
    return {"cleared": session_id}


@app.get("/api/history")
async def history(session_id: str):
    return {"session_id": session_id, "history": brain.get_history(session_id)}


@app.get("/api/memory")
async def memory_context_endpoint(q: str = ""):
    """Show the memory context that would be injected for query `q`.

    Used for transparency — see exactly what the agent will "remember"
    before answering. No LLM call is made; this just builds the same
    memory context brain.py uses.
    """
    import memory
    ctx = memory.build_memory_context(q or "general context")
    sections = {}
    current = None
    buf = []
    for line in ctx.split("\n"):
        if line.startswith("## "):
            if current:
                sections[current] = "\n".join(buf).strip()
            current = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()
    return {
        "query": q,
        "total_chars": len(ctx),
        "sections": sections,
        "sessions_on_disk": memory.session_count(),
    }


@app.get("/api/context")
async def live_context_endpoint(q: str = ""):
    """Show what live cross-surface context would be loaded for query `q`.

    Combines:
      - skills_bridge.build_skills_context(q)  → active + triggered skills
      - telegram_sync.get_full_context()       → recent Telegram updates

    No LLM call; just inspection of the actual context blocks brain.py would inject.
    """
    import skills_bridge
    import telegram_sync

    query = q or "general"
    skills_ctx = skills_bridge.build_skills_context(query)
    tg_ctx = telegram_sync.get_full_context(limit_chars=1500)
    _tg_dir = Path(
        os.environ.get("AVA_STATE_DIR")
        or (Path.home() / ".config" / "ai_agent_chat" / "telegram_sync")
    )
    tg_state_file = _tg_dir / "state.json"
    cache_file = _tg_dir / "cache.json"
    return {
        "query": query,
        "skills_chars": len(skills_ctx),
        "skills_preview": skills_ctx[:1500],
        "telegram_chars": len(tg_ctx),
        "telegram_preview": tg_ctx[:1500],
        "telegram_state_exists": tg_state_file.exists(),
        "telegram_cache_exists": cache_file.exists(),
        "available_skill_count": len(skills_bridge.list_skill_names()),
    }


@app.get("/static/{filename}")
async def static_file(filename: str):
    """Serve a small static asset (the logo).

    The bridge is a single-file app with no StaticFiles mount, so this
    one route covers the handful of images it needs.
    """
    from fastapi.responses import FileResponse
    _static_dir = Path(__file__).parent / "static"
    # Reject any path traversal attempt.
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="bad filename")
    p = _static_dir / filename
    if not p.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(p)


@app.get("/api/health")
async def health():
    """Bridge health + version + memory layer status."""
    import memory
    return {
        "status": "ok",
        "service": "sam-bridge",
        "version": app.version,
        "memory_layer": {
            "soul_md": bool(memory.SOUL_PATH.exists()),
            "sam_index": bool(memory.SAM_INDEX_PATH.exists()),
            "fact_store_db": memory.FACT_STORE_DB.exists(),
            "life_memory_dir": memory.LONG_TERM_MEMORY_DIR.exists(),
            "sessions_on_disk": memory.session_count(),
        },
        "tts_chain": f"kokoro ({kokoro_tts._resolve_voice()}) primary, piper fallback",
        "stt": "whisper.cpp on 127.0.0.1:8178",
        # Which build this is: "prod" or "dev".
        "channel": APP_CHANNEL,
        "build": APP_VERSION,
        "telegram_poller": _telegram_poller_status(),
        # v0.10 — barge-in (full-duplex interrupt) status
        "barge_in": {
            "enabled": BARGE_IN_ENABLED,
            "capture_max_ms": BARGE_CAPTURE_MAX_MS,
            "endpoint_silence_blocks": BARGE_ENDPOINT_SILENCE_BLOCKS,
            "trigger_ceiling": barge.TRIGGER_CEILING,
        },
    }


def _telegram_poller_status() -> dict:
    """Inspect telegram_sync state for /api/health."""
    try:
        import telegram_sync
        return {
            "bot_token_loaded": bool(telegram_sync.BOT_TOKEN),
            "home_channel_loaded": bool(telegram_sync.HOME_CHANNEL_ID),
            "poller_running": telegram_sync._poller_thread is not None
                              and telegram_sync._poller_thread.is_alive(),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ── Barge-in (full-duplex) support ────────────────────────────────────
#
# v0.10: the widget can now be interrupted mid-reply. Previously the WS
# handler only read the next client message AFTER the brain finished, so
# the user could not talk over Sam at all — the words just sat unread in
# the socket.
#
# How it works:
#   * The client streams raw PCM16 16kHz mono frames over the SAME
#     WebSocket (message type "pcm") while it is in barge mode. The
#     MediaRecorder webm path is untouched and still used for the
#     normal push-to-talk STT.
#   * A background reader task owns the socket during a turn, feeds
#     every frame into barge.BargeDetector, and raises a trip when the
#     user starts speaking.
#   * The reply loop checks the trip flag between sentences and aborts.
#
# The detector is a port of Hermes' own _BargeDetector (see barge.py
# header). It is calibrated from the quiet room BEFORE any TTS starts,
# so Sam's own voice leaking into the phone mic never trips it.

BARGE_IN_ENABLED = os.environ.get("AVA_BARGE_IN", "").strip() == "1"
# How long the reader keeps waiting for the user to finish after a trip.
BARGE_CAPTURE_MAX_MS = int(os.environ.get("AVA_BARGE_CAPTURE_MAX_MS", "8000"))
# Silence (below the floor) that ends the captured utterance.
BARGE_ENDPOINT_SILENCE_BLOCKS = int(os.environ.get("AVA_BARGE_ENDPOINT_BLOCKS", "12"))


class _BargeState:
    """Per-connection barge-in state.

    Lives for the duration of one turn. The reader task fills it; the
    reply loop polls it.
    """

    def __init__(self, playing_flag=None, tag: str = "?") -> None:
        # v0.10.4 — a label so the log says WHICH state object tripped.
        # There are several in play (collect phase, reply reader,
        # carried-interrupt turn) and without this it is impossible to
        # tell which one fired.
        self.tag = tag
        self.detector = barge.BargeDetector()
        self.preroll = barge.PreRollBuffer(max_blocks=40)  # ~1.2s of pre-roll
        self.tripped: str | None = None      # "generation" | "playback"
        self.captured: list[bytes] = []      # PCM after the trip
        # A shared one-element list, so synth_and_ship can flip "Sam is
        # speaking" while the detector reads it. None means never playing.
        self._playing_flag = playing_flag
        self.playing = False                 # is TTS currently playing?
        self.active = False                  # is the reader task live?
        self.user_done = False               # client says the user stopped talking
        self.barge_off = False               # client turned barge mode off
        self.muted = False                   # v0.10.5 — client muted its mic
        self._frames_in = 0
        self._levels: list[int] = []   # v0.10.4 — level trace buffer
        self._triggers: list[int] = []

    def feed(self, pcm: bytes) -> None:
        """Feed one PCM chunk. Raises the trip on first detection."""
        if self.tripped is not None:
            # Still capturing the user's utterance after the trip.
            self.captured.append(pcm)
            return
        if self._playing_flag is not None:
            self.playing = self._playing_flag[0]
        for frame in barge.frames_from_pcm(pcm):
            self._frames_in += 1
            self.preroll.push(frame)
            rms = barge.rms_int16(frame)
            # v0.10.4 — level trace. The phone's mic levels are the
            # only way to tune the thresholds; without seeing the real
            # numbers we are guessing.
            if os.environ.get("AVA_BARGE_TRACE", "").strip() == "1":
                _trg = max(
                    self.detector.quiet_floor * self.detector.mult,
                    barge.PLAYBACK_MIN_TRIGGER if self.playing
                    else float(barge.SILENCE_RMS_THRESHOLD) * 2,
                )
                self._levels.append(round(rms))
                self._triggers.append(int(_trg))
                if len(self._levels) >= 10:
                    log.info(
                        "barge rms[%s] trig[%s] playing=%s floor=%.0f",
                        " ".join(str(v) for v in self._levels),
                        " ".join(str(v) for v in self._triggers),
                        self.playing, self.detector.quiet_floor,
                    )
                    self._levels = []
                    self._triggers = []
            phase = self.detector.feed(rms, playing=self.playing)
            if phase and self.playing:
                log.info("ws barge-in: tripped during PLAYBACK")
            if phase:
                self.tripped = phase
                # Pre-roll + the tripping frame: don't clip the first syllable.
                pre = self.preroll.take()
                self.captured.append(pre)
                log.info(
                    "ws barge-in TRIPPED phase=%s after %d frames (state=%s)",
                    phase, self._frames_in, self.tag,
                )
                return

    def capture_pcm(self) -> bytes:
        """The user's utterance, pre-roll included."""
        return b"".join(self.captured)

    def endpoint(self, pcm: bytes) -> bytes:
        """Trim the captured utterance at the first sustained silence.

        After a trip we keep collecting audio. The user is done talking
        when the level drops back to the quiet floor for a few
        consecutive blocks. Returns the PCM up to (and excluding) that
        silence, so we transcribe only the actual utterance.

        Two details that matter for real speech:
          * The quiet counter only starts once we've SEEN speech. The
            first frames of a capture are often near-silent (the trip
            pre-roll), and counting those would endpoint immediately.
          * The threshold is relative to the loudest frame in the
            capture, not the detector's quiet floor. Real speech has
            inter-word gaps that dip well below the ambient floor but
            are still clearly speech; a floor-relative threshold cuts
            the utterance in half.

        If the user never goes quiet, we return everything we captured.
        """
        if not pcm:
            return b""
        frames = barge.frames_from_pcm(pcm)
        if not frames:
            return pcm
        levels = [barge.rms_int16(f) for f in frames]
        peak = max(levels) if levels else 0.0
        if peak <= 0:
            return b""
        # Endpoint threshold: 22% of the peak, floored at the silence
        # threshold so a truly quiet capture isn't chopped arbitrarily.
        threshold = max(peak * 0.22, float(barge.SILENCE_RMS_THRESHOLD))
        quiet_run = 0
        seen_speech = False
        kept: list[bytes] = []
        for frame, rms in zip(frames, levels):
            if rms >= threshold:
                seen_speech = True
                quiet_run = 0
                kept.append(frame)
                continue
            if not seen_speech:
                # Leading silence — keep it (it's the pre-roll) but don't
                # count it toward the endpoint.
                kept.append(frame)
                continue
            quiet_run += 1
            if quiet_run >= BARGE_ENDPOINT_SILENCE_BLOCKS:
                break  # endpoint found
            kept.append(frame)
        out = b"".join(kept)
        log.info(
            "ws barge-in: endpointed %d → %d bytes (peak=%.0f thresh=%.0f, "
            "%d frames kept, %d quiet)",
            len(pcm), len(out), peak, threshold, len(kept), quiet_run,
        )
        return out


async def _barge_reader(
    websocket: WebSocket,
    state: _BargeState,
    inbound: asyncio.Queue,
) -> None:
    """Background task that reads client messages during a turn.

    The main WS loop is busy streaming the brain reply, so it cannot
    read the socket itself. This task does, and routes PCM frames into
    the detector. It exits when the turn ends.
    """
    log.info("ws barge reader: started (active=%s)", state.active)
    _n = 0
    try:
        while state.active:
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(), timeout=0.25
                )
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                log.info("ws barge reader: receive ended: %s", e)
                break
            _n += 1
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")
            if kind == "pcm":
                import base64
                try:
                    chunk = base64.b64decode(msg.get("data_b64", ""))
                except Exception:
                    continue
                if chunk:
                    state.feed(chunk)
            elif kind == "mute":
                # v0.10.5 — the client muted its mic. No PCM is coming, so
                # the server must not treat the silence as end-of-speech
                # and must not start a turn on stale audio.
                state.muted = bool(msg.get("value"))
                if state.muted:
                    state.user_done = False
                    state.captured = []
                    state.tripped = None
                    state.detector.recent_above.clear()
                log.info(
                    "ws barge reader: mic %s",
                    "muted" if state.muted else "unmuted",
                )
            elif kind == "playing":
                # v0.10.4 — the client tells us when audio actually starts
                # and stops playing. This is the signal the VAD needs to
                # pick the bleed-safe playback threshold.
                #
                # The flag lives on the state (state._playing_flag) so this
                # works for any reader, not just the one that has the
                # closure variable.
                _playing = bool(msg.get("value"))
                if state._playing_flag is not None:
                    state._playing_flag[0] = _playing
                state.playing = _playing
            elif kind == "duplex_off":
                # v0.10.4 — the client ended the duplex session.
                state.barge_off = True
                state.user_done = True
                break
            elif kind == "barge_stop":
                # The client finished its own utterance (it detected
                # end-of-speech on-device). Signal the turn machinery.
                #
                # v0.10.4 — DO NOT BREAK. The reader used to exit here,
                # which meant it stopped consuming PCM the moment the
                # user finished their first sentence. Everything the user
                # said while Sam was replying went into the socket
                # unread, so the detector could never trip and Sam
                # talked over them. The reader now lives for the whole
                # session.
                state.user_done = True
                log.info("ws barge reader: user finished speaking")
                # Deliberately no break — keep watching the mic.
            elif kind == "barge_off":
                state.user_done = True
                state.barge_off = True
                break
    except Exception as e:  # noqa: BLE001
        log.warning("ws barge reader ended: %s", e)


def _ws_client_alive(websocket: WebSocket) -> bool:
    """True if the WebSocket still has a live connection.

    Starlette's client_state goes to DISCONNECTED once the peer closes.
    Checking it before we send lets us stop a reply that nobody is
    listening to, instead of burning a TTS synth per remaining sentence.
    """
    try:
        from starlette.websockets import WebSocketState
        return websocket.client_state == WebSocketState.CONNECTED
    except Exception:
        return True  # be permissive if we can't tell


def _start_barge_reader(websocket: WebSocket, playing_flag=None):
    """Start the barge-in reader for this turn.

    Returns (barge_state, reader_task). If barge-in is disabled, both
    are None/None and the caller behaves exactly as before.
    """
    if not BARGE_IN_ENABLED:
        return None, None
    state = _BargeState(playing_flag=playing_flag, tag="ptt-reply")
    state.active = True
    inbound: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(_barge_reader(websocket, state, inbound))
    return state, task


async def _finish_barge(
    websocket: WebSocket,
    barge_state,
    reader_task,
    reply_text: str,
) -> None:
    """Stop the barge reader and, if the user interrupted, tell the client.

    Must be called on every turn — it is what guarantees the reader task
    never outlives the turn and never keeps the socket busy.
    """
    if barge_state is None:
        return
    barge_state.active = False
    if reader_task is not None:
        reader_task.cancel()
        try:
            await reader_task
        except (asyncio.CancelledError, Exception):
            pass
    if barge_state.tripped:
        try:
            await websocket.send_json({
                "type": "barge_in",
                "phase": barge_state.tripped,
                "reply_text": reply_text,
            })
        except Exception as e:  # noqa: BLE001
            log.warning("ws barge_in notify failed: %s", e)
        log.info(
            "ws barge-in: interrupted after %d chars (%s)",
            len(reply_text or ""), barge_state.tripped,
        )


async def _capture_barge_utterance(
    websocket: WebSocket,
    barge_state,
    reader_task,
    reply_text: str,
) -> str:
    """After an interrupt: collect the rest of the user's utterance,
    endpoint it, transcribe it, and return the text.

    This is what makes the loop actually duplex: you interrupt Sam, keep
    talking, and your full sentence becomes the next turn — no extra
    button press.

    Returns "" if the user said nothing intelligible.
    """
    if barge_state is None or not barge_state.tripped:
        return ""

    # Keep collecting until the user goes quiet (endpoint) or we hit the
    # capture ceiling. The reader task is still alive at this point, so
    # it keeps feeding frames into state.captured.
    deadline = time.time() + (BARGE_CAPTURE_MAX_MS / 1000.0)
    while time.time() < deadline:
        await asyncio.sleep(0.05)
        if barge_state.tripped is None:
            break
        # Endpointed? If the last ~1s of captured audio is quiet, the
        # user is done.
        tail = barge_state.captured[-33:] if len(barge_state.captured) >= 33 else barge_state.captured
        if len(tail) >= 20:
            tail_pcm = b"".join(tail)
            frames = barge.frames_from_pcm(tail_pcm)
            if frames:
                thresh = max(
                    barge_state.detector.quiet_floor * 1.8,
                    barge.SILENCE_RMS_THRESHOLD * 2,
                )
                if all(barge.rms_int16(f) < thresh for f in frames):
                    break

    pcm = barge_state.capture_pcm()
    if not pcm:
        await _finish_barge(websocket, barge_state, reader_task, reply_text)
        return ""

    trimmed = barge_state.endpoint(pcm)
    if len(trimmed) < barge.BLOCK_BYTES * 5:  # < 150ms of audio — too short
        await _finish_barge(websocket, barge_state, reader_task, reply_text)
        return ""

    # Transcribe off the event loop — whisper-server is a blocking HTTP call.
    loop = asyncio.get_event_loop()
    try:
        text = await loop.run_in_executor(
            None, whisper_stt.transcribe_wav_pcm, trimmed, barge.SAMPLE_RATE
        )
    except Exception as e:  # noqa: BLE001
        log.warning("ws barge-in STT failed: %s", e)
        text = ""

    await _finish_barge(websocket, barge_state, reader_task, reply_text)

    text = (text or "").strip()
    if text:
        try:
            await websocket.send_json({
                "type": "transcript",
                "final": text,
                "stt_ms": 0,
                "barge": True,
            })
        except Exception as e:  # noqa: BLE001
            log.warning("ws barge transcript send failed: %s", e)
        log.info("ws barge-in: user said %r", text[:80])
    return text


async def _duplex_turn(
    websocket: WebSocket,
    barge_state,
    sid: str,
    synth_and_ship,
) -> str:
    """Run one duplex turn: the user spoke, now Sam answers.

    The barge reader has been collecting PCM this whole time. When the
    client tells us the user stopped talking, we endpoint the audio,
    transcribe it, and run the brain. Returns the reply text.

    This is the heart of duplex mode: no button, no explicit message —
    just speech.
    """
    if barge_state is None:
        return ""

    # Wait for the client to say the user is done (or the capture cap).
    deadline = time.time() + (BARGE_CAPTURE_MAX_MS / 1000.0)
    while time.time() < deadline and not barge_state.user_done:
        await asyncio.sleep(0.03)
        if barge_state.barge_off:
            return ""

    pcm = barge_state.capture_pcm()
    if not pcm:
        return ""

    trimmed = barge_state.endpoint(pcm)
    if len(trimmed) < barge.BLOCK_BYTES * 8:  # < 240ms — too short
        log.info("ws duplex: utterance too short (%d bytes), ignoring", len(trimmed))
        return ""

    loop = asyncio.get_event_loop()
    try:
        text = await loop.run_in_executor(
            None, whisper_stt.transcribe_wav_pcm, trimmed, barge.SAMPLE_RATE
        )
    except Exception as e:  # noqa: BLE001
        log.warning("ws duplex STT failed: %s", e)
        return ""

    text = (text or "").strip()
    if not text:
        log.info("ws duplex: STT returned nothing")
        return ""

    log.info("ws duplex: user said %r", text[:80])
    try:
        await websocket.send_json({
            "type": "transcript", "final": text, "stt_ms": 0, "duplex": True,
        })
    except Exception as e:  # noqa: BLE001
        log.warning("ws duplex transcript send failed: %s", e)

    await websocket.send_json({"type": "state", "state": "thinking"})

    async def on_sentence(sent: str):
        await synth_and_ship(sent)

    full = await _stream_brain_to_sentences(
        text, sid, on_sentence, barge_state=barge_state,
    )
    brain.append_turn(sid, text, full)
    return full


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    """WebSocket for real-time push-to-talk.

    Wire protocol (v0.8):

      client → server:
        {"type": "start", "session_id": "..."}    open a turn
        {"type": "audio", "data_b64": "..."}       raw 16kHz mono s16le PCM chunk
        {"type": "stop"}                            end of utterance → run brain
        {"type": "text", "text": "...", "session_id": "..."}  text-only turn
        {"type": "confirm", "decision": "yes"|"no"}  resolve pending action
        {"type": "ping"}

      server → client:
        {"type": "state", "state": "listening"|"thinking"|"speaking"|"idle"}
        {"type": "transcript", "partial": "...", "final": "..."}  STT progress
        {"type": "text", "role": "sam", "text": "...", "stream": true}
        {"type": "audio", "format": "wav", "sample_rate": 22050,
         "data_b64": "..."}        one sentence of Piper audio at a time
        {"type": "pending_confirm", "tool": "...", "summary": "..."}
        {"type": "tool_call", ...}
        {"type": "done", "stop_reason": "...", "audio_chunks": N}
        {"type": "pong"}
    """
    await websocket.accept()
    log.info("ws: client connected")
    sid: str = f"ava-{uuid.uuid4().hex[:8]}"
    # v0.10 — a one-element list used as a mutable "Sam is speaking" flag.
    # synth_and_ship sets it while audio is in flight so the barge
    # detector can pick the right (bleed-safe) threshold. A list is used
    # because the flag must be shared with the nested closure.
    _barge_playing_flag: list[bool] = [False]
    # audio_chunks holds base64-decoded webm/mp4 bytes from the browser
    # MediaRecorder. We accumulate them, then run whisper_stt.transcribe
    # on the concatenated blob on `stop`.
    audio_chunks: list[bytes] = []
    audio_mime: str = "audio/webm"
    loop = asyncio.get_event_loop()

    async def synth_and_ship(sentence: str) -> None:
        """Fire Piper on a single sentence and ship both the text delta
        and the WAV over WS so the UI can render text + queue audio."""
        # v0.10.1 — if the client disconnected, don't waste a TTS synth on
        # a sentence nobody will hear. Raising here aborts the brain
        # stream at the next sentence boundary.
        if not _ws_client_alive(websocket):
            raise WebSocketDisconnect()
        # v0.10 — tell the barge detector that Sam is audibly speaking, so
        # it uses the playback threshold (bleed-safe) instead of the
        # generation one. Without this, a barge during playback is
        # mis-classified as "generation".
        if _barge_playing_flag is not None:
            _barge_playing_flag[0] = True
        try:
            # Send the text delta first so the UI shows it word-by-word
            await websocket.send_json({
                "type": "text", "role": "sam", "text": sentence + " ",
                "stream": True,
            })
            wav, provider = await loop.run_in_executor(
                None, _synth_with_fallback, sentence
            )
            if wav:
                import base64
                # Use the real sample rate of whatever provider synthesized,
                # not the hardcoded Piper rate. iOS Safari is picky about
                # decodeAudioData when WAV header SR ≠ actual SR.
                sr = piper_tts.SAMPLE_RATE if provider == "piper" else KOKORO_SAMPLE_RATE
                await websocket.send_json({
                    "type": "audio",
                    "format": "wav",
                    "sample_rate": sr,
                    "data_b64": base64.b64encode(wav).decode("ascii"),
                    "sentence": sentence,
                    "provider": provider,
                })
                log.info(
                    "ws synth: sent sentence=%r provider=%s sr=%d bytes=%d",
                    sentence[:40], provider, sr, len(wav),
                )
                # v0.10.1 — heartbeat. A long reply can be silent for 20+
                # seconds while the brain thinks or TTS renders, and
                # carriers/proxies drop idle sockets. This tiny frame
                # keeps the connection warm.
                await websocket.send_json({
                    "type": "keepalive", "ts": time.time(),
                })
        except Exception as e:
            log.warning("ws synth failed for sentence %r: %s", sentence[:40], e)
        finally:
            if _barge_playing_flag is not None:
                _barge_playing_flag[0] = False

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")

            if kind == "ping":
                await websocket.send_json({"type": "pong", "ts": time.time()})
                continue

            if kind == "duplex":
                # ── v0.10 duplex session ──────────────────────────────
                # The client opened a persistent mic and will stream
                # raw PCM frames for the whole conversation. We loop
                # here: collect speech → transcribe → answer → repeat,
                # until the client sends {"type":"duplex_off"}.
                #
                # We own the socket in this loop and read the frames
                # ourselves (no separate reader task) so we always know
                # exactly what the user said.
                sid = msg.get("session_id") or sid
                await websocket.send_json({"type": "state", "state": "listening"})
                # v0.10.4 — ONE reader for the whole duplex session.
                #
                # The reader owns the socket from the moment the session
                # opens until it closes. There is no gap between the
                # collect phase and the reply phase, so a trip detected
                # during collection is still visible when the reply
                # starts, and the reply's reader is already watching
                # before the first sentence ships.
                #
                # Before v0.10.4 the collect phase and the reply each
                # had their own _BargeState, so they could not see each
                # other's trips. That is why the user's speech was
                # detected and then ignored, and Sam talked over them.
                duplex_reader_state = _BargeState(
                    playing_flag=_barge_playing_flag, tag="duplex"
                )
                duplex_reader_state.active = True
                duplex_reader_state.user_done = True  # ready for the first utterance
                duplex_reader_queue: asyncio.Queue = asyncio.Queue()
                duplex_reader_task = asyncio.create_task(
                    _barge_reader(
                        websocket, duplex_reader_state, duplex_reader_queue
                    )
                )
                # The collect phase and the reply share ONE state object.
                duplex_barge_state = duplex_reader_state
                log.info("ws duplex: session reader started")
                # v0.10.3 — speech captured during an interrupt, carried
                # into the next turn so the user doesn't have to repeat.
                duplex_interrupt_text = ""

                try:
                    while True:
                        # ── 1. collect one utterance ──
                        #
                        # v0.10.4 — reset the per-turn capture state but
                        # KEEP the shared detector and the trip flag.
                        # Before v0.10.4 this created a fresh detector and
                        # a fresh state object per turn, which is how the
                        # collect phase and the reply ended up blind to
                        # each other's trips.
                        duplex_barge_state.user_done = False
                        duplex_barge_state.captured = []
                        duplex_barge_state.preroll = barge.PreRollBuffer(
                            max_blocks=40
                        )
                        # Keep the detector's learned quiet floor (it is
                        # calibrated from the room and should persist for
                        # the session) but clear the trip.
                        duplex_barge_state.tripped = None
                        duplex_barge_state.detector.recent_above.clear()

                        # v0.10.3 — if the user interrupted the last reply,
                        # their speech IS this turn. Don't make them say
                        # it again.
                        if duplex_interrupt_text:
                            carried = duplex_interrupt_text
                            duplex_interrupt_text = ""
                            log.info(
                                "ws duplex: using carried interrupt %r",
                                carried[:60],
                            )
                            await websocket.send_json({
                                "type": "transcript", "final": carried,
                                "stt_ms": 0, "duplex": True,
                            })
                            await websocket.send_json({
                                "type": "state", "state": "thinking",
                            })

                            async def on_sentence_c(sent: str):
                                await synth_and_ship(sent)

                            duplex_barge_state.captured = []
                            duplex_barge_state.preroll = barge.PreRollBuffer(
                                max_blocks=40
                            )
                            duplex_barge_state.tripped = None
                            duplex_barge_state.detector.recent_above.clear()
                            carried_reply = await _stream_brain_to_sentences(
                                carried, sid, on_sentence_c,
                                barge_state=duplex_barge_state,
                            )
                            brain.append_turn(sid, carried, carried_reply)
                            await websocket.send_json({
                                "type": "done", "stop_reason": "end_turn",
                                "reply_text": carried_reply,
                                "reply_len": len(carried_reply),
                                "total_ms": 0,
                            })
                            await websocket.send_json({
                                "type": "state", "state": "listening",
                            })
                            continue

                        # Wait for the client to say the user is done.
                        #
                        # v0.10.4 — the session reader owns the socket and
                        # feeds the detector. This loop must NOT read
                        # the socket itself: two readers on one socket
                        # race, and the collect loop would steal the PCM
                        # frames before the detector saw them (which is
                        # exactly the bug — the trip fired on a state
                        # object the reply could not see).
                        got = False
                        deadline = time.time() + 30.0
                        while time.time() < deadline:
                            if duplex_barge_state.barge_off:
                                break
                            if duplex_barge_state.user_done:
                                got = True
                                break
                            await asyncio.sleep(0.02)

                        if duplex_barge_state.barge_off:
                            log.info("ws duplex: client ended the session")
                            await websocket.send_json({"type": "state", "state": "idle"})
                            # Tear down the session reader.
                            duplex_reader_state.active = False
                            duplex_reader_task.cancel()
                            try:
                                await duplex_reader_task
                            except (asyncio.CancelledError, Exception):
                                pass
                            break
                        # v0.10.5 — the client muted its mic. Do not run a
                        # turn on the silence; wait for it to unmute.
                        if duplex_barge_state.muted:
                            await asyncio.sleep(0.1)
                            continue
                        if not got:
                            # Timed out waiting for speech — keep waiting.
                            continue

                        # ── 2. endpoint + transcribe ──
                        pcm = duplex_barge_state.capture_pcm()
                        if not pcm:
                            continue
                        trimmed = duplex_barge_state.endpoint(pcm)
                        if len(trimmed) < barge.BLOCK_BYTES * 8:
                            continue

                        loop2 = asyncio.get_event_loop()
                        try:
                            user_text = await loop2.run_in_executor(
                                None, whisper_stt.transcribe_wav_pcm,
                                trimmed, barge.SAMPLE_RATE,
                            )
                        except Exception as e:  # noqa: BLE001
                            log.warning("ws duplex STT failed: %s", e)
                            continue
                        user_text = (user_text or "").strip()
                        if not user_text:
                            continue
                        log.info("ws duplex: user said %r", user_text[:80])
                        await websocket.send_json({
                            "type": "transcript", "final": user_text,
                            "stt_ms": 0, "duplex": True,
                        })

                        # ── 3. answer ──
                        await websocket.send_json({"type": "state", "state": "thinking"})

                        async def on_sentence_d(sent: str):
                            await synth_and_ship(sent)

                        # Re-arm for this answer so the user can interrupt
                        # it too.
                        #
                        # v0.10.4 — do NOT create a new detector here. The
                        # detector and the trip flag are SHARED with the
                        # collect phase (one reader for the whole duplex
                        # session). Replacing the detector here is what
                        # made the reply blind to a trip that had already
                        # fired — the user's speech was detected and then
                        # thrown away, and Sam talked over them.
                        duplex_barge_state.captured = []
                        duplex_barge_state.preroll = barge.PreRollBuffer(
                            max_blocks=40
                        )
                        duplex_barge_state.tripped = None
                        duplex_barge_state.detector.recent_above.clear()

                        # v0.10.4 — START THE READER BEFORE THE BRAIN.
                        #
                        # v0.10.3 started the reader, but the ordering was
                        # wrong: the reader was created AFTER the brain
                        # had already begun streaming, so the first
                        # sentences shipped before the detector was
                        # watching. The tagged log showed it:
                        #
                        #   TRIPPED (state=duplex-collect)   <- user's
                        #                                         speech,
                        #                                         detected
                        #                                         in the
                        #                                         COLLECT
                        #                                         phase
                        #   reply reader started
                        #   reply starting, trip already set? None
                        #   TRIPPED (state=duplex-reply)      <- too late
                        #
                        # Two problems:
                        #  1. The collect phase and the reply used
                        #     DIFFERENT state objects, so a trip in the
                        #     collect phase was invisible to the reply.
                        #  2. The reader only started once the brain was
                        #     already talking, so the first sentences
                        #     were un-interruptible.
                        #
                        # Fix: the reader runs for the WHOLE duplex
                        # session — one state object, started once, so
                        # there is no gap between "collecting" and
                        # "replying". The trip is therefore always
                        # visible to whichever phase is running.
                        #
                        # The collect loop's own reads are replaced by
                        # the reader's state, so both phases look at the
                        # same detector.

                        # (the reader is created once, outside the loop —
                        #  see the code above this block)
                        reply = await _stream_brain_to_sentences(
                            user_text, sid, on_sentence_d,
                            barge_state=duplex_barge_state,
                        )

                        # v0.10.5 — DO NOT cancel the reader here.
                        #
                        # The reader is the SESSION reader: it owns the
                        # socket for the whole duplex session so the
                        # collect phase and the reply share one detector
                        # (v0.10.4). Cancelling it at the end of a reply
                        # left nobody reading the socket, so the next
                        # turn's PCM frames — and the mute signal — were
                        # never seen. The session reader is torn down
                        # when the session ends, not per turn.
                        brain.append_turn(sid, user_text, reply)

                        # v0.10.1 — if the phone went away mid-reply, stop.
                        # Without this the server kept synthesizing every
                        # remaining sentence (69 of them in one observed
                        # case) and threw "Cannot call send once a close
                        # message has been sent" for each one.
                        if not _ws_client_alive(websocket):
                            log.info(
                                "ws duplex: client gone mid-reply — abandoning "
                                "%d chars", len(reply),
                            )
                            raise WebSocketDisconnect()

                        # v0.10.2 — build the /api/audio fallback for duplex
                        # too. The PTT path has always sent it, and it is
                        # the recovery path for the case where iOS Safari
                        # silently drops in-flight WS audio frames. Without
                        # it in duplex, a dropped frame means the reply
                        # is simply lost — no audio, no recovery.
                        #
                        # The client only PLAYS it when it received no WS
                        # audio frames, so this costs one extra synth but
                        # never causes a double-play.
                        duplex_audio_url = None
                        # v0.10.8 — skip the fallback for an INTERRUPTED
                        # reply. The user cut Sam off; replaying the whole
                        # reply from the start is wrong, and it is what put
                        # a second copy of the reply on top of the
                        # interrupted one. (Observed in the logs:
                        # "tts: 104.17s audio for 1585 chars" synthesized
                        # for a reply that was cut after 12 seconds.)
                        #
                        # The fallback is only for the case where the
                        # phone silently dropped the WS frames on an
                        # UNINTERRUPTED reply.
                        if reply.strip() and not (
                            duplex_barge_state is not None
                            and duplex_barge_state.tripped
                        ):
                            try:
                                wav_bytes, _prov = await loop2.run_in_executor(
                                    None, _synth_with_fallback, reply
                                )
                                if wav_bytes:
                                    import uuid as _uuid
                                    audio_id = (
                                        f"{int(time.time()*1000)}-"
                                        f"{_uuid.uuid4().hex[:6]}.wav"
                                    )
                                    _AUDIO_CACHE[audio_id] = wav_bytes
                                    if len(_AUDIO_CACHE) > 50:
                                        oldest = next(iter(_AUDIO_CACHE))
                                        _AUDIO_CACHE.pop(oldest, None)
                                    duplex_audio_url = f"/api/audio/{audio_id}"
                                    log.info(
                                        "ws duplex: fallback audio %d bytes → %s",
                                        len(wav_bytes), duplex_audio_url,
                                    )
                            except Exception as e:  # noqa: BLE001
                                log.warning("ws duplex fallback synth failed: %s", e)

                        if duplex_barge_state.tripped:
                            await websocket.send_json({
                                "type": "barge_in",
                                "phase": duplex_barge_state.tripped,
                                "reply_text": reply,
                            })
                            log.info(
                                "ws duplex: interrupted after %d chars (%s)",
                                len(reply), duplex_barge_state.tripped,
                            )
                            # v0.10.3 — carry the interrupting speech into
                            # the next turn. The user talked over Sam; that
                            # speech is the new request. Stash it on the
                            # loop so the next collect phase uses it
                            # instead of waiting for the user to speak
                            # again (which they shouldn't have to - they
                            # already spoke).
                            duplex_interrupt_pcm = (
                                duplex_barge_state.capture_pcm()
                            )
                            duplex_interrupt_text = ""
                            if duplex_interrupt_pcm:
                                trimmed_i = duplex_barge_state.endpoint(
                                    duplex_interrupt_pcm
                                )
                                if len(trimmed_i) >= barge.BLOCK_BYTES * 8:
                                    try:
                                        duplex_interrupt_text = (
                                            await loop2.run_in_executor(
                                                None,
                                                whisper_stt.transcribe_wav_pcm,
                                                trimmed_i,
                                                barge.SAMPLE_RATE,
                                            )
                                        or ""
                                        ).strip()
                                    except Exception as e:  # noqa: BLE001
                                        log.warning(
                                            "ws duplex interrupt STT failed: %s", e
                                        )
                                        duplex_interrupt_text = ""
                            if duplex_interrupt_text:
                                log.info(
                                    "ws duplex: interrupt carried %r",
                                    duplex_interrupt_text[:80],
                                )
                                await websocket.send_json({
                                    "type": "transcript",
                                    "final": duplex_interrupt_text,
                                    "stt_ms": 0,
                                    "duplex": True,
                                    "interrupt": True,
                                })

                        duplex_done = {
                            "type": "done",
                            "stop_reason": "end_turn",
                            "reply_text": reply,
                            "reply_len": len(reply),
                            "total_ms": 0,
                        }
                        if duplex_audio_url:
                            duplex_done["audio_url"] = duplex_audio_url
                            duplex_done["audio_size"] = len(
                                _AUDIO_CACHE.get(duplex_audio_url.split("/")[-1], b"")
                            )
                        await websocket.send_json(duplex_done)
                        await websocket.send_json({"type": "state", "state": "listening"})
                except WebSocketDisconnect:
                    log.info("ws duplex: client disconnected")
                    # Tear down the session reader.
                    duplex_reader_state.active = False
                    duplex_reader_task.cancel()
                    try:
                        await duplex_reader_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    raise
                continue

            if kind == "start":
                sid = msg.get("session_id") or sid
                audio_chunks = []
                audio_mime = msg.get("mime") or "audio/webm"
                await websocket.send_json({
                    "type": "state", "state": "listening",
                    "session_id": sid,
                })
                continue

            if kind == "audio":
                # Browser sends base64-encoded webm/mp4 MediaRecorder chunks
                # at ~10/sec (timeslice=100ms). We accumulate; server-side
                # STT runs on the concatenated blob at `stop`.
                import base64
                try:
                    chunk = base64.b64decode(msg.get("data_b64", ""))
                    if chunk:
                        audio_chunks.append(chunk)
                except Exception as e:
                    log.warning("ws audio chunk decode failed: %s", e)
                continue

            if kind == "stop":
                # User released the button. Concatenate audio chunks,
                # run STT, then stream the brain reply back sentence-by-
                # sentence with Piper chunks interleaved.
                await websocket.send_json({"type": "state", "state": "thinking"})
                t0 = time.time()
                user_text = ""
                if audio_chunks:
                    blob = b"".join(audio_chunks)
                    log.info(
                        "ws stop: %d chunks, %d bytes, mime=%s",
                        len(audio_chunks), len(blob), audio_mime,
                    )
                    try:
                        # whisper.cpp server rejects non-WAV (HTTP 400). The
                        # browser sends webm/opus from MediaRecorder, so we
                        # transcode to 16k mono PCM/WAV with ffmpeg first.
                        import subprocess, tempfile
                        wav_bytes = blob
                        if "wav" not in audio_mime.lower():
                            with tempfile.NamedTemporaryFile(
                                suffix="\.webm", delete=False
                            ) as tf_in, tempfile.NamedTemporaryFile(
                                suffix=".wav", delete=False
                            ) as tf_out:
                                tf_in.write(blob)
                                tf_in.flush()
                                tf_out_path = tf_out.name
                            subprocess.run(
                                [
                                    "ffmpeg", "-y", "-loglevel", "error",
                                    "-i", tf_in.name,
                                    "-ac", "1", "-ar", "16000",
                                    "-f", "wav", tf_out_path,
                                ],
                                check=True,
                            )
                            with open(tf_out_path, "rb") as f:
                                wav_bytes = f.read()
                            os.unlink(tf_in.name)
                            os.unlink(tf_out_path)
                            log.info(
                                "ws transcoded webm→wav: %d → %d bytes",
                                len(blob), len(wav_bytes),
                            )
                        user_text = await loop.run_in_executor(
                            None, whisper_stt.transcribe, wav_bytes
                        )
                    except Exception as e:
                        log.exception("ws whisper failed")
                        user_text = ""
                audio_chunks = []
                stt_ms = int((time.time() - t0) * 1000)

                if not user_text.strip():
                    await websocket.send_json({
                        "type": "transcript", "partial": "", "final": "",
                        "stt_ms": stt_ms,
                    })
                    await websocket.send_json({
                        "type": "text", "role": "sam",
                        "text": "I didn't catch that. Try again?",
                    })
                    await websocket.send_json({
                        "type": "done", "stop_reason": "end_turn",
                        "reply_text": "I didn't catch that. Try again?",
                        "reply_len": 31,
                        "total_ms": int((time.time() - t0) * 1000),
                    })
                    await websocket.send_json({"type": "state", "state": "idle"})
                    continue

                await websocket.send_json({
                    "type": "transcript", "final": user_text, "stt_ms": stt_ms,
                })

                # Handle confirmation cycle on a held action
                pending = brain.get_pending_confirm(sid)
                if pending:
                    decision = brain._looks_like_confirmation(user_text)
                    if decision == "yes":
                        result = brain.resolve_pending(sid, "yes")
                        reply_text = _format_confirmation_result(pending, result)
                        await websocket.send_json({
                            "type": "text", "role": "sam", "text": reply_text,
                        })
                        await synth_and_ship(reply_text)
                        brain.append_turn(sid, user_text, reply_text)
                        await websocket.send_json({
                            "type": "done", "stop_reason": "end_turn",
                            "reply_text": reply_text, "reply_len": len(reply_text),
                            "total_ms": int((time.time() - t0) * 1000),
                        })
                        await websocket.send_json({"type": "state", "state": "idle"})
                        continue
                    elif decision == "no":
                        brain.resolve_pending(sid, "no")
                        reply_text = f"Cancelled. I won't {pending['summary']}."
                        await websocket.send_json({
                            "type": "text", "role": "sam", "text": reply_text,
                        })
                        await synth_and_ship(reply_text)
                        brain.append_turn(sid, user_text, reply_text)
                        await websocket.send_json({
                            "type": "done", "stop_reason": "end_turn",
                            "reply_text": reply_text, "reply_len": len(reply_text),
                            "total_ms": int((time.time() - t0) * 1000),
                        })
                        await websocket.send_json({"type": "state", "state": "idle"})
                        continue
                    else:
                        brain.resolve_pending(sid, "no")  # off-topic → drop

                # Stream the brain reply sentence-by-sentence.
                # Capture non-text events so we can relay them to the client.
                non_text_events: list[dict] = []

                async def on_sentence(sent: str):
                    await synth_and_ship(sent)

                # Run the streaming brain
                await websocket.send_json({"type": "state", "state": "speaking"})

                # v0.10 — start the barge-in reader BEFORE the brain runs so
                # the user can interrupt the moment Sam starts talking. The
                # reader owns the socket while we stream; the main loop's
                # next `receive_text()` is not reached until this turn ends.
                barge_state, reader_task = _start_barge_reader(
                    websocket, playing_flag=_barge_playing_flag
                )
                full = await _stream_brain_to_sentences(
                    user_text, sid, on_sentence, barge_state=barge_state,
                )

                # v0.10 — if the user interrupted, capture the rest of what
                # they said and run it as the next turn. This is the duplex
                # loop: interrupt → keep talking → Sam answers.
                barge_text = ""
                if barge_state is not None and barge_state.tripped:
                    barge_text = await _capture_barge_utterance(
                        websocket, barge_state, reader_task, full,
                    )
                else:
                    await _finish_barge(
                        websocket, barge_state, reader_task, full
                    )
                brain.append_turn(sid, user_text, full)

                # v0.9.3 fallback: also send a single /api/audio/<id>.wav URL
                # containing the full reply. iOS Safari's WebSocket drops
                # large base64 audio frames silently; the legacy <audio> URL
                # path is reliable. Client plays whichever arrives first.
                audio_url = None
                if full.strip():
                    try:
                        wav_bytes, _provider = await loop.run_in_executor(
                            None, _synth_with_fallback, full
                        )
                        if wav_bytes:
                            import uuid as _uuid
                            audio_id = f"{int(time.time()*1000)}-{_uuid.uuid4().hex[:6]}.wav"
                            _AUDIO_CACHE[audio_id] = wav_bytes
                            if len(_AUDIO_CACHE) > 50:
                                oldest = next(iter(_AUDIO_CACHE))
                                _AUDIO_CACHE.pop(oldest, None)
                            audio_url = f"/api/audio/{audio_id}"
                            log.info(
                                "ws fallback audio: %d bytes → %s",
                                len(wav_bytes), audio_url,
                            )
                    except Exception as e:
                        log.warning("ws fallback synth failed: %s", e)

                # Send any deferred tool/confirm events
                held = brain.get_pending_confirm(sid)
                if held:
                    log.info("ws: sending pending_confirm for sid=%s tool=%s", sid, held["tool"])
                    await websocket.send_json({
                        "type": "pending_confirm",
                        "tool": held["tool"], "summary": held["summary"],
                    })

                done_payload = {
                    "type": "done",
                    "stop_reason": "end_turn",
                    "reply_text": full,
                    "reply_len": len(full),
                    "total_ms": int((time.time() - t0) * 1000),
                }
                if audio_url:
                    done_payload["audio_url"] = audio_url
                    done_payload["audio_size"] = len(_AUDIO_CACHE.get(audio_url.split("/")[-1], b""))
                await websocket.send_json(done_payload)
                await websocket.send_json({"type": "state", "state": "idle"})

                # v0.10 — duplex continue. The user interrupted and kept
                # talking; their utterance becomes the next turn without
                # another button press.
                if barge_text:
                    log.info("ws duplex: continuing with %r", barge_text[:60])
                    user_text = barge_text
                    t0 = time.time()
                    await websocket.send_json({"type": "state", "state": "thinking"})
                    barge_state, reader_task = _start_barge_reader(websocket)
                    full = await _stream_brain_to_sentences(
                        user_text, sid, on_sentence, barge_state=barge_state,
                    )
                    barge_text = ""
                    if barge_state is not None and barge_state.tripped:
                        barge_text = await _capture_barge_utterance(
                            websocket, barge_state, reader_task, full,
                        )
                    else:
                        await _finish_barge(
                            websocket, barge_state, reader_task, full
                        )
                    brain.append_turn(sid, user_text, full)
                    await websocket.send_json({
                        "type": "done",
                        "stop_reason": "end_turn",
                        "reply_text": full,
                        "reply_len": len(full),
                        "total_ms": int((time.time() - t0) * 1000),
                    })
                    await websocket.send_json({"type": "state", "state": "idle"})
                continue

            if kind == "text":
                # Pure text path (no audio). Reuse streaming brain.
                text = msg.get("text", "").strip()
                if not text:
                    continue
                sid = msg.get("session_id") or sid
                t0 = time.time()
                await websocket.send_json({"type": "state", "state": "thinking"})

                # Confirm-cycle handling
                pending = brain.get_pending_confirm(sid)
                if pending:
                    decision = brain._looks_like_confirmation(text)
                    if decision == "yes":
                        result = brain.resolve_pending(sid, "yes")
                        reply_text = _format_confirmation_result(pending, result)
                        await websocket.send_json({
                            "type": "text", "role": "sam", "text": reply_text,
                        })
                        await synth_and_ship(reply_text)
                        brain.append_turn(sid, text, reply_text)
                        await websocket.send_json({
                            "type": "done", "stop_reason": "end_turn",
                            "reply_text": reply_text, "reply_len": len(reply_text),
                            "total_ms": int((time.time() - t0) * 1000),
                        })
                        await websocket.send_json({"type": "state", "state": "idle"})
                        continue
                    elif decision == "no":
                        brain.resolve_pending(sid, "no")
                        reply_text = f"Cancelled. I won't {pending['summary']}."
                        await websocket.send_json({
                            "type": "text", "role": "sam", "text": reply_text,
                        })
                        await synth_and_ship(reply_text)
                        brain.append_turn(sid, text, reply_text)
                        await websocket.send_json({
                            "type": "done", "stop_reason": "end_turn",
                            "reply_text": reply_text, "reply_len": len(reply_text),
                            "total_ms": int((time.time() - t0) * 1000),
                        })
                        await websocket.send_json({"type": "state", "state": "idle"})
                        continue
                    else:
                        brain.resolve_pending(sid, "no")

                async def on_sentence_text(sent: str):
                    await synth_and_ship(sent)

                await websocket.send_json({"type": "state", "state": "speaking"})

                # v0.10 — same barge-in wiring as the audio path. The text
                # path is a separate branch, so it needs its own reader.
                barge_state, reader_task = _start_barge_reader(
                    websocket, playing_flag=_barge_playing_flag
                )
                full = await _stream_brain_to_sentences(
                    text, sid, on_sentence_text, barge_state=barge_state,
                )
                await _finish_barge(websocket, barge_state, reader_task, full)
                brain.append_turn(sid, text, full)
                held = brain.get_pending_confirm(sid)
                if held:
                    await websocket.send_json({
                        "type": "pending_confirm",
                        "tool": held["tool"], "summary": held["summary"],
                    })
                await websocket.send_json({
                    "type": "done",
                    "stop_reason": "end_turn",
                    "reply_text": full,
                    "reply_len": len(full),
                    "total_ms": int((time.time() - t0) * 1000),
                })
                await websocket.send_json({"type": "state", "state": "idle"})
                continue

    except WebSocketDisconnect:
        log.info("ws: client disconnected")


def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 16000) -> bytes:
    """Wrap raw s16le mono PCM in a minimal WAV header so whisper_stt.transcribe
    (which expects WAV/webm/m4a) accepts the rolling PCM buffer from the
    browser MediaStream."""
    import struct
    data_size = len(pcm_bytes)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,    # PCM
        1,    # mono
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        data_size,
    )
    return header + pcm_bytes


if __name__ == "__main__":
    import uvicorn

    # Optional HTTPS — iOS Safari blocks getUserMedia on http:// origins,
    # so we serve HTTPS when cert files are present. Set AVA_HTTPS=0 to
    # force plain HTTP (e.g. for local dev).
    cert_path = os.environ.get("AGENT_VOICE_TLS_CERT", os.path.join(os.path.dirname(__file__), "certs", "server.crt"))
    key_path = os.environ.get("AGENT_VOICE_TLS_KEY", os.path.join(os.path.dirname(__file__), "certs", "server.key"))
    use_https = os.environ.get("AGENT_VOICE_HTTPS", "1") != "0" and os.path.exists(cert_path) and os.path.exists(key_path)

    if use_https:
        log.info("starting sam bridge (HTTPS) on %s:%d", HOST, PORT)
        log.info("open https://<your-host>:%d/  (TLS cert)", PORT)
        log.info("open https://<your-host>:%d/  (IP — cert won't match, use hostname on iOS)", PORT)
        # Pre-warm Kokoro in a background thread so the first /api/chat is fast.
        import threading
        threading.Thread(target=_warm_kokoro, daemon=True).start()
        # Prime Telegram context cache and start the poller so the voice agent
        # has fresh cross-surface context without a manual prompt.
        try:
            import telegram_sync
            threading.Thread(target=telegram_sync.prime_cache, daemon=True).start()
            if telegram_sync.start_poller():
                log.info("telegram_sync: poller started")
        except Exception as e:
            log.warning("telegram_sync startup failed: %s", e)
        uvicorn.run(
            app, host=HOST, port=PORT, log_level="info", access_log=False,
            ssl_certfile=cert_path, ssl_keyfile=key_path,
            # v0.10.1 — uvicorn's default ws_ping_timeout is 20s. A long
            # reply (brain thinking, then dozens of TTS sentences) blocks
            # the event loop long enough that the server can't answer a
            # ping, and uvicorn closes the connection. That killed the
            # socket mid-reply — the client saw Sam's answer cut off.
            # We send our own keepalive frames between sentences, so
            # the ping timeout is redundant; disable it and keep the
            # connection open for the whole reply.
            ws_ping_interval=None,
            ws_ping_timeout=None,
        )
    else:
        log.info("starting agent-voice-ai bridge (HTTP) on %s:%d", HOST, PORT)
        log.info("open http://localhost:%d/  (or http://localhost:%d/)", PORT, PORT)
        import threading
        try:
            import telegram_sync
            threading.Thread(target=telegram_sync.prime_cache, daemon=True).start()
            telegram_sync.start_poller()
        except Exception as e:
            log.warning("telegram_sync startup failed: %s", e)
        uvicorn.run(
            app, host=HOST, port=PORT, log_level="info", access_log=False,
            # v0.10.1 — see the note in the HTTPS branch above.
            ws_ping_interval=None,
            ws_ping_timeout=None,
        )
