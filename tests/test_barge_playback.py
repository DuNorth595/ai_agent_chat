# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""Playback-phase barge test: cut Sam off MID-SENTENCE while he speaks.

The earlier A/B test tripped during "generation" (before any audio
shipped). This test proves the other, more important phase: Sam is
audibly speaking, the user talks over him, and the server cuts the
TTS mid-stream with phase="playback".

The trick: wait until the server has actually SENT audio frames
(type "audio"), then start talking.

Run: python tests/test_barge_playback.py
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
FRAME_SAMPLES = 480


def pcm_frame(amplitude, n=FRAME_SAMPLES, freq=440):
    out = bytearray()
    for i in range(n):
        v = int(amplitude * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        out += struct.pack("<h", v)
    return bytes(out)


SAMPLE_RATE = 16000


async def main():
    require_piper()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    audio_frames = 0
    barge = None
    barge_phase = None
    reply_at_barge = ""

    async with websockets.connect(WS_URL, ssl=ctx, max_size=None) as ws:
        await ws.send(json.dumps({"type": "start", "session_id": "pb-1"}))
        await ws.send(json.dumps({
            "type": "text",
            "text": "Write a long detailed essay about the history of the Roman Empire.",
            "session_id": "pb-1",
        }))

        speaking = asyncio.Event()
        got_audio = asyncio.Event()

        async def reader():
            nonlocal audio_frames, barge, barge_phase, reply_at_barge
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    t = msg.get("type")
                    if t == "state" and msg.get("state") == "speaking":
                        speaking.set()
                    elif t == "audio":
                        audio_frames += 1
                        if audio_frames == 1:
                            got_audio.set()
                    elif t == "barge_in":
                        barge = msg
                        barge_phase = msg.get("phase")
                        reply_at_barge = msg.get("reply_text") or ""
                        return
            except Exception:
                pass

        rtask = asyncio.get_event_loop().create_task(reader())

        # Wait for Sam to start speaking.
        try:
            await asyncio.wait_for(speaking.wait(), timeout=25)
        except asyncio.TimeoutError:
            print("  FAIL: never reached 'speaking'")
            return

        # Wait for actual AUDIO to be shipped — that's the playback phase.
        print("  waiting for the first audio frame…")
        try:
            await asyncio.wait_for(got_audio.wait(), timeout=25)
        except asyncio.TimeoutError:
            print("  FAIL: no audio frames shipped")
            return
        print(f"  audio frame #1 received ({audio_frames} so far)")

        # Let a couple more sentences out so the barge lands mid-stream.
        await asyncio.sleep(1.5)
        print(f"  {audio_frames} audio frames sent — now talking over Sam")

        # Calibrate with silence first.
        silence = pcm_frame(0)
        for _ in range(40):
            await ws.send(json.dumps({
                "type": "pcm", "data_b64": base64.b64encode(silence).decode(),
            }))
            await asyncio.sleep(0.005)

        # LOUD — trip the detector in the playback phase.
        loud = pcm_frame(9000, freq=300)
        for _ in range(30):
            if rtask.done():
                break
            await ws.send(json.dumps({
                "type": "pcm", "data_b64": base64.b64encode(loud).decode(),
            }))
            await asyncio.sleep(0.005)

        try:
            await asyncio.wait_for(rtask, timeout=25)
        except asyncio.TimeoutError:
            print("  FAIL: no barge_in within 25s")

    print()
    print("=" * 64)
    print(f"  audio frames before barge : {audio_frames}")
    print(f"  barge_in received        : {barge is not None}")
    print(f"  barge phase              : {barge_phase}")
    print(f"  reply text at barge      : {reply_at_barge[:120]!r}")
    print("=" * 64)
    ok = barge is not None and barge_phase == "playback" and audio_frames > 0
    print("  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


asyncio.run(main())
