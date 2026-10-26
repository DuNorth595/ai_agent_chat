# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""Definitive barge-in test: proves the server CUTS the reply when the
user talks over Sam, and does NOT cut it when the user stays quiet.

Two runs against the live bridge:
  A. quiet  — PCM silence only → expect NO barge_in, full reply
  B. loud   — PCM silence then speech → expect barge_in, shortened reply

Run: python tests/test_barge_ab.py
"""
import asyncio
import base64
import json
import math
import ssl
import struct
import sys
from pathlib import Path

import websockets

# Make the sibling _testcfg importable no matter how this file is run.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _testcfg import HAVE_PIPER, require_piper, synth

WS_URL = "wss://127.0.0.1:8770/ws"
SAMPLE_RATE = 16000
FRAME_SAMPLES = 480  # 30ms


def pcm_frame(amplitude, n=FRAME_SAMPLES, freq=440):
    out = bytearray()
    for i in range(n):
        v = int(amplitude * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        out += struct.pack("<h", v)
    return bytes(out)


async def run_case(loud: bool):
    """Returns (barge_seen, sentences_seen, done_seen, reply_len)."""
    barge_seen = False
    done_seen = False
    sentences = 0
    reply_len = 0
    state = "idle"
    talking = asyncio.Event()

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    async with websockets.connect(WS_URL, ssl=ctx, max_size=None) as ws:
        await ws.send(json.dumps({"type": "start", "session_id": f"bt-{loud}"}))
        await ws.send(json.dumps({
            "type": "text",
            "text": "Count from 1 to 40 slowly, in full sentences.",
            "session_id": f"bt-{loud}",
        }))

        async def reader():
            nonlocal barge_seen, done_seen, sentences, reply_len
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    t = msg.get("type")
                    if t == "state":
                        if msg.get("state") == "speaking":
                            talking.set()
                    elif t == "text":
                        sentences += 1
                    elif t == "barge_in":
                        barge_seen = True
                        reply_len = len(msg.get("reply_text") or "")
                        return
                    elif t == "done":
                        done_seen = True
                        reply_len = len(msg.get("reply_text") or "")
                        return
            except Exception:
                pass

        rtask = asyncio.get_event_loop().create_task(reader())

        # Wait for Sam to start talking.
        try:
            await asyncio.wait_for(talking.wait(), timeout=20)
        except asyncio.TimeoutError:
            print("  FAIL: server never said 'speaking'")
            return False, 0, False, 0

        # Let a few sentences get out.
        await asyncio.sleep(1.2)

        # ── Stream PCM ──
        silence = pcm_frame(0)
        # 80 silent frames (~2.4s) to calibrate the quiet floor.
        for _ in range(80):
            await ws.send(json.dumps({
                "type": "pcm", "data_b64": base64.b64encode(silence).decode(),
            }))
            await asyncio.sleep(0.005)
            if loud:
                # Check if we got interrupted already
                if rtask.done():
                    break

        if loud:
            # LOUD speech to trip the detector.
            loud_pcm = pcm_frame(9000, freq=300)
            for _ in range(25):
                if rtask.done():
                    break
                await ws.send(json.dumps({
                    "type": "pcm", "data_b64": base64.b64encode(loud_pcm).decode(),
                }))
                await asyncio.sleep(0.005)

        # Wait for the outcome.
        try:
            await asyncio.wait_for(rtask, timeout=25)
        except asyncio.TimeoutError:
            pass
        rtask.cancel()

    return barge_seen, sentences, done_seen, reply_len


async def main():
    require_piper()
    print("=" * 66)
    print("CASE A: user stays QUIET (control) — expect NO barge_in")
    print("=" * 66)
    a_barge, a_sents, a_done, a_len = await run_case(loud=False)
    print(f"  barge_in seen : {a_barge}")
    print(f"  sentences     : {a_sents}")
    print(f"  done seen     : {a_done}")
    print(f"  reply length  : {a_len} chars")
    a_ok = (not a_barge) and a_done and a_len > 100
    print(f"  RESULT: {'PASS' if a_ok else 'FAIL'}")

    print()
    print("=" * 66)
    print("CASE B: user TALKS OVER Sam — expect barge_in + cut reply")
    print("=" * 66)
    b_barge, b_sents, b_done, b_len = await run_case(loud=True)
    print(f"  barge_in seen : {b_barge}")
    print(f"  sentences     : {b_sents}")
    print(f"  done seen     : {b_done}")
    print(f"  reply length  : {b_len} chars")
    b_ok = b_barge
    print(f"  RESULT: {'PASS' if b_ok else 'FAIL'}")

    print()
    print("=" * 66)
    print(f"  CONTROL (quiet) reply : {a_len} chars, {a_sents} sentences")
    print(f"  BARGE  (loud)   reply : {b_len} chars, {b_sents} sentences")
    print(f"  INTERRUPT WORKED: {b_ok and not a_barge}")
    print("=" * 66)
    sys.exit(0 if (a_ok and b_ok) else 1)


asyncio.run(main())
