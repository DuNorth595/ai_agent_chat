# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""Test for the double-play bug (v0.10.2).

The symptom that was reported: "Something still kills your initial reply
and it has to repeat itself. The repeat finishes the responses cleanly."

Root cause: the client's `done` handler played the /api/audio fallback
UNCONDITIONALLY. So the sentence-by-sentence WS audio played, and then
the fallback replayed the ENTIRE reply from the start — the user heard
it twice.

The fix: only use the fallback when NO WS audio frames were received.

This test asserts the SERVER contract that makes the client decision
possible:
  * `done` carries an `audio_url` (the fallback still exists)
  * the client can tell how many audio frames it got

It can't drive the browser, so it verifies the pieces the client
depends on, and greps the served JS for the guard.

Run: python tests/test_no_double_play.py   (or: source .venv/bin/activate)
"""
import asyncio
import base64
import json
import math
import re
import ssl
import struct
import subprocess
import sys
from pathlib import Path
import urllib.request
import wave

import websockets

# Make the sibling _testcfg importable no matter how this file is run.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _testcfg import HAVE_PIPER, require_piper, synth

WS_URL = "wss://127.0.0.1:8770/ws"
FRAME_SAMPLES = 480




def silence():
    return b"\x00" * (FRAME_SAMPLES * 2)


async def main():
    require_piper()
    ok = True

    # ── 1. The served page must contain the guard ──
    ctx2 = ssl.create_default_context()
    ctx2.check_hostname = False
    ctx2.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        "https://127.0.0.1:8770/", headers={"User-Agent": "test"}
    )
    page = urllib.request.urlopen(req, context=ctx2, timeout=15).read().decode(
        "utf-8", "ignore"
    )

    checks = [
        ("_wsAudioFramesReceived is tracked", "_wsAudioFramesReceived" in page),
        ("fallback skipped when WS audio played",
         "ws audio already played" in page),
        ("fallback used when no WS audio",
         "no ws audio frames" in page),
        ("counter resets per turn",
         re.search(r'msg\.state === "thinking"[\s\S]{0,120}_wsAudioFramesReceived = 0',
                   page) is not None),
        ("counter increments on audio frames",
         re.search(r'_wsAudioFramesReceived\+\+', page) is not None),
    ]
    print("=" * 66)
    print("1. Served page contains the double-play guard")
    for label, passed in checks:
        print(f"   {'✓' if passed else '✗'} {label}")
        if not passed:
            ok = False

    # ── 2. The server must still send an audio_url in `done` ──
    print()
    print("=" * 66)
    print("2. Server still provides the fallback in `done`")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    audio_frames = 0
    done_payload = {}

    async with websockets.connect(WS_URL, ssl=ctx, max_size=None,
                                  ping_interval=None) as ws:
        await ws.send(json.dumps({"type": "duplex", "session_id": "dp-1"}))
        pcm = synth("Say hello in one short sentence.", "/tmp/dp.wav")
        for _ in range(25):
            await ws.send(json.dumps({
                "type": "pcm", "data_b64": base64.b64encode(silence()).decode(),
            }))
            await asyncio.sleep(0.005)
        for i in range(0, len(pcm) - FRAME_SAMPLES * 2 + 1, FRAME_SAMPLES * 2):
            await ws.send(json.dumps({
                "type": "pcm",
                "data_b64": base64.b64encode(pcm[i:i + FRAME_SAMPLES * 2]).decode(),
            }))
            await asyncio.sleep(0.030)
        for _ in range(12):
            await ws.send(json.dumps({
                "type": "pcm", "data_b64": base64.b64encode(silence()).decode(),
            }))
            await asyncio.sleep(0.01)
        await ws.send(json.dumps({"type": "barge_stop"}))

        deadline = asyncio.get_event_loop().time() + 180
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            if msg.get("type") == "audio":
                audio_frames += 1
            elif msg.get("type") == "done":
                done_payload = msg
                break

    print(f"   audio frames received : {audio_frames}")
    print(f"   done.audio_url        : {bool(done_payload.get('audio_url'))}")
    print(f"   done.reply_len        : {done_payload.get('reply_len')}")
    # A very short reply can produce no complete sentence, and therefore
    # no TTS frame at all. That is not a broken WS path — it is a short
    # answer. Only flag it if the reply was substantial enough that
    # audio frames were expected.
    if audio_frames < 1:
        reply_len = done_payload.get("reply_len") or 0
        if reply_len > 60:
            print("   ✗ no audio frames on a substantial reply — WS path broken")
            ok = False
        else:
            print(f"   ✓ reply was short ({reply_len} chars) — no audio expected")
    else:
        print("   ✓ WS audio path delivered frames")
    if not done_payload.get("audio_url"):
        print("   ✗ no audio_url in done — the fallback is gone entirely")
        print("     (it must EXIST for the case where iOS drops WS frames)")
        ok = False
    else:
        print("   ✓ fallback still available (used only if WS audio was dropped)")

    print()
    print("=" * 66)
    print("  RESULT:", "PASS" if ok else "FAIL")
    print("=" * 66)
    sys.exit(0 if ok else 1)


asyncio.run(main())
