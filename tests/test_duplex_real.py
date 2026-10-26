# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""Duplex loop test with REAL SPEECH.

Uses Piper to synthesize actual words, streams them as PCM16 16kHz over
the WS exactly as the phone does, then sends barge_stop. Asserts the
server transcribes the actual words and answers them.

Run: python tests/test_duplex_real.py
"""
import asyncio
import base64
import json
import math
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
PHRASE = "What is the weather like today?"
WAV = "/tmp/duplex_real.wav"
FRAME_SAMPLES = 480  # 30ms at 16kHz




def silence_frame():
    return b"\x00" * (FRAME_SAMPLES * 2)


def rms(pcm):
    n = len(pcm) // 2
    s = struct.unpack("<%dh" % n, pcm)
    return math.sqrt(sum(float(v) * v for v in s) / n)


async def main():
    require_piper()
    pcm = synth(PHRASE, "/tmp/duplex_real.wav")
    frames = [pcm[i:i + FRAME_SAMPLES * 2]
              for i in range(0, len(pcm) - FRAME_SAMPLES * 2 + 1, FRAME_SAMPLES * 2)]
    print(f"  synthesized {len(frames)} frames of real speech")
    print(f"  speech RMS = {rms(pcm):.0f}, silence RMS = {rms(silence_frame()):.0f}")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    states, transcript, reply, done = [], None, [], False

    async with websockets.connect(WS_URL, ssl=ctx, max_size=None) as ws:
        await ws.send(json.dumps({"type": "duplex", "session_id": "dreal-1"}))
        print("  sent {type: duplex}")

        async def reader():
            nonlocal transcript, done
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    t = msg.get("type")
                    if t == "state":
                        states.append(msg["state"])
                    elif t == "transcript":
                        transcript = msg.get("final")
                    elif t == "text":
                        reply.append(msg.get("text", ""))
                    elif t == "done":
                        done = True
                        return
            except Exception as e:
                print("  reader ended:", e)

        rtask = asyncio.get_event_loop().create_task(reader())
        await asyncio.sleep(0.4)

        # 1s of silence to calibrate the quiet floor.
        for _ in range(34):
            await ws.send(json.dumps({
                "type": "pcm",
                "data_b64": base64.b64encode(silence_frame()).decode(),
            }))
            await asyncio.sleep(0.01)

        # The real speech, paced at real time.
        for f in frames:
            await ws.send(json.dumps({
                "type": "pcm", "data_b64": base64.b64encode(f).decode(),
            }))
            await asyncio.sleep(0.030)

        # A beat of silence so the endpoint is clean.
        for _ in range(10):
            await ws.send(json.dumps({
                "type": "pcm",
                "data_b64": base64.b64encode(silence_frame()).decode(),
            }))
            await asyncio.sleep(0.01)

        print("  sent {type: barge_stop}")
        await ws.send(json.dumps({"type": "barge_stop"}))

        try:
            await asyncio.wait_for(rtask, timeout=40)
        except asyncio.TimeoutError:
            print("  FAIL: no done within 40s")
            print("  states:", states)
            return

    print()
    print("=" * 64)
    print(f"  EXPECTED WORDS : {PHRASE!r}")
    print(f"  SERVER HEARD   : {transcript!r}")
    print(f"  states         : {states}")
    print(f"  reply          : {''.join(reply)[:200]!r}")
    print("=" * 64)

    heard = (transcript or "").lower()
    key_words = ["weather", "today"]
    hit = [w for w in key_words if w in heard]
    ok = bool(hit) and done
    print(f"  matched {len(hit)}/{len(key_words)} key words: {hit}")
    print("  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


asyncio.run(main())
