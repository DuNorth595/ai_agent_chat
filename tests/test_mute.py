# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""Mute test — does muting actually stop Sam hearing you?

The mute button in duplex must:
  1. stop the client shipping PCM
  2. tell the server, so the server does not treat the silence as
     end-of-speech and does not run a turn on stale audio
  3. let the session continue afterwards (unmute → talk again)

Run: python tests/test_mute.py   (or: source .venv/bin/activate)
"""
import asyncio
import base64
import json
import os
import ssl
import struct
import subprocess
import sys
from pathlib import Path
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
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    transcripts = []
    states = []
    done_count = 0

    async with websockets.connect(WS_URL, ssl=ctx, max_size=None,
                                  ping_interval=None) as ws:
        await ws.send(json.dumps({"type": "duplex", "session_id": "mute-1"}))

        async def reader():
            nonlocal done_count
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    t = msg.get("type")
                    if t == "transcript":
                        transcripts.append(msg.get("final"))
                    elif t == "state":
                        states.append(msg.get("state"))
                    elif t == "done":
                        done_count += 1
            except Exception:
                pass

        asyncio.get_event_loop().create_task(reader())
        await asyncio.sleep(0.4)

        # ── 1. speak normally ──
        pcm = synth("Hello, are you there?", "/tmp/mute_hello.wav")
        step = FRAME_SAMPLES * 2
        for _ in range(20):
            await ws.send(json.dumps({
                "type": "pcm",
                "data_b64": base64.b64encode(silence()).decode(),
            }))
            await asyncio.sleep(0.005)
        for i in range(0, len(pcm) - step + 1, step):
            await ws.send(json.dumps({
                "type": "pcm", "data_b64": base64.b64encode(pcm[i:i + step]).decode(),
            }))
            await asyncio.sleep(0.030)
        for _ in range(12):
            await ws.send(json.dumps({
                "type": "pcm", "data_b64": base64.b64encode(silence()).decode(),
            }))
            await asyncio.sleep(0.01)
        await ws.send(json.dumps({"type": "barge_stop"}))
        print("  turn 1 sent — waiting for the answer")

        deadline = asyncio.get_event_loop().time() + 90
        while done_count < 1 and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.2)
        print(f"  turn 1 answered: {len(transcripts)} transcript(s)")

        # ── 2. MUTE ──
        await ws.send(json.dumps({"type": "mute", "value": True}))
        print("  muted")
        await asyncio.sleep(1.0)

        # While muted, the client sends NO PCM. Give the server plenty of
        # time to (incorrectly) start a turn on the silence.
        before = done_count
        await asyncio.sleep(12)
        after = done_count
        print(f"  turns while muted: {after - before} (should be 0)")

        # ── 3. UNMUTE and speak again ──
        await ws.send(json.dumps({"type": "mute", "value": False}))
        print("  unmuted — speaking again")
        await asyncio.sleep(0.5)

        pcm2 = synth("And now I am audible again.", "/tmp/mute_again.wav")
        for i in range(0, len(pcm2) - step + 1, step):
            await ws.send(json.dumps({
                "type": "pcm", "data_b64": base64.b64encode(pcm2[i:i + step]).decode(),
            }))
            await asyncio.sleep(0.030)
        for _ in range(12):
            await ws.send(json.dumps({
                "type": "pcm", "data_b64": base64.b64encode(silence()).decode(),
            }))
            await asyncio.sleep(0.01)
        await ws.send(json.dumps({"type": "barge_stop"}))

        deadline = asyncio.get_event_loop().time() + 90
        while done_count < 2 and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.2)

    print()
    print("=" * 66)
    print(f"  transcripts     : {transcripts}")
    print(f"  total turns     : {done_count}")
    print("=" * 66)

    ok = True
    if len(transcripts) < 1:
        print("  ✗ turn 1 was never answered")
        ok = False
    if after - before != 0:
        print(f"  ✗ the server ran {after - before} turn(s) while muted")
        ok = False
    else:
        print("  ✓ no turn ran while muted")
    if len(transcripts) < 2:
        print("  ✗ the post-unmute turn was never answered")
        ok = False
    else:
        print("  ✓ speaking works again after unmuting")

    print("  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


asyncio.run(main())
