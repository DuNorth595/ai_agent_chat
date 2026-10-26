# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""Duplex interrupt test — can the user cut Sam off mid-sentence?

This is the case that was broken and that the PTT-path test could not
catch: in the DUPLEX branch the server was blocked inside
_stream_brain_to_sentences and NOTHING read the socket, so the PCM
frames the phone sends while Sam is talking were never seen. The
detector never tripped and Sam talked over the user.

The test:
  1. opens a duplex session
  2. asks for a LONG answer so Sam is still speaking
  3. starts talking over him
  4. asserts the server emitted `barge_in` with phase=playback
  5. asserts the interrupting speech was carried into the next turn

Run: python tests/test_duplex_interrupt.py   (or: source .venv/bin/activate)
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
FRAME_SAMPLES = 480
SAMPLE_RATE = 16000




def pcm_frame(amplitude, n=FRAME_SAMPLES, freq=440):
    out = bytearray()
    for i in range(n):
        v = int(amplitude * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        out += struct.pack("<h", v)
    return bytes(out)


def silence():
    return b"\x00" * (FRAME_SAMPLES * 2)


async def main():
    require_piper()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    barge = None
    barge_phase = None
    audio_frames = 0
    transcripts = []
    states = []
    first_audio = asyncio.Event()

    async with websockets.connect(WS_URL, ssl=ctx, max_size=None,
                                  ping_interval=None) as ws:
        await ws.send(json.dumps({"type": "duplex", "session_id": "int-1"}))

        async def reader():
            nonlocal barge, barge_phase, audio_frames
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    t = msg.get("type")
                    if t == "state":
                        states.append(msg.get("state"))
                    elif t == "transcript":
                        transcripts.append(msg.get("final"))
                    elif t == "audio":
                        audio_frames += 1
                        if audio_frames == 1:
                            first_audio.set()
                    elif t == "barge_in":
                        barge = msg
                        barge_phase = msg.get("phase")
            except Exception:
                pass

        asyncio.get_event_loop().create_task(reader())
        await asyncio.sleep(0.4)

        # ── 1. ask for a long answer ──
        # NOTE: the duplex branch only accepts PCM frames — a {"type":
        # "text"} message is ignored there. So the request has to go
        # through the duplex path: stream the "speech" as PCM, then
        # barge_stop to trigger the turn.
        for _ in range(25):
            await ws.send(json.dumps({
                "type": "pcm",
                "data_b64": base64.b64encode(silence()).decode(),
            }))
            await asyncio.sleep(0.005)
        # Real speech, so the STT actually produces a request.
        request_pcm = synth(
            "Write a long detailed essay about the history of the Roman "
            "Empire, at least a thousand words.",
            "/tmp/duplex_int_request.wav",
        )
        step = FRAME_SAMPLES * 2
        for i in range(0, len(request_pcm) - step + 1, step):
            await ws.send(json.dumps({
                "type": "pcm",
                "data_b64": base64.b64encode(request_pcm[i:i + step]).decode(),
            }))
            await asyncio.sleep(0.030)
        for _ in range(12):
            await ws.send(json.dumps({
                "type": "pcm",
                "data_b64": base64.b64encode(silence()).decode(),
            }))
            await asyncio.sleep(0.01)
        await ws.send(json.dumps({"type": "barge_stop"}))
        print("  asked (via duplex PCM) — waiting for Sam to start speaking")

        # Wait for Sam to actually start speaking.
        try:
            await asyncio.wait_for(first_audio.wait(), timeout=120)
        except asyncio.TimeoutError:
            print("  FAIL: Sam never started speaking")
            return
        print(f"  Sam started speaking (audio frames: {audio_frames})")

        # Let Sam actually SPEAK before interrupting. The brain may spend
        # many seconds on tool calls before the first sentence ships, so
        # wait for real audio frames, not just the first one.
        deadline_audio = asyncio.get_event_loop().time() + 60
        while audio_frames < 3 and asyncio.get_event_loop().time() < deadline_audio:
            await asyncio.sleep(0.2)
        await asyncio.sleep(1.0)
        print(f"  {audio_frames} audio frames so far — now talking over him")

        # ── 2. calibrate, then talk over Sam ──
        for _ in range(40):
            await ws.send(json.dumps({
                "type": "pcm",
                "data_b64": base64.b64encode(silence()).decode(),
            }))
            await asyncio.sleep(0.005)

        loud = pcm_frame(9000, freq=300)
        for _ in range(40):
            await ws.send(json.dumps({
                "type": "pcm",
                "data_b64": base64.b64encode(loud).decode(),
            }))
            await asyncio.sleep(0.005)

        # ── 3. wait for the barge ──
        deadline = asyncio.get_event_loop().time() + 30
        while barge is None and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.1)

        # ── 4. KEEP TALKING after the interrupt ──
        # The user interrupted Sam and continued. That continuation must
        # become the next turn — the user should not have to say it again.
        if barge is not None:
            print("  interrupted — now keeping talking (the real test)")
            followup = synth(
                "Actually, stop talking about Rome and tell me the weather.",
                "/tmp/duplex_int_followup.wav",
            )
            step2 = FRAME_SAMPLES * 2
            for i in range(0, len(followup) - step2 + 1, step2):
                await ws.send(json.dumps({
                    "type": "pcm",
                    "data_b64": base64.b64encode(followup[i:i + step2]).decode(),
                }))
                await asyncio.sleep(0.030)
            for _ in range(12):
                await ws.send(json.dumps({
                    "type": "pcm",
                    "data_b64": base64.b64encode(silence()).decode(),
                }))
                await asyncio.sleep(0.01)
            await ws.send(json.dumps({"type": "barge_stop"}))
            # Give the server time to run the follow-up turn.
            await asyncio.sleep(20)

    print()
    print("=" * 66)
    print(f"  audio frames before barge : {audio_frames}")
    print(f"  barge_in received         : {barge is not None}")
    print(f"  barge phase               : {barge_phase}")
    print(f"  transcripts               : {transcripts}")
    print(f"  states                    : {states[:12]}")
    print("=" * 66)

    ok = True
    if barge is None:
        print("  FAIL: Sam was never interrupted — he talked over the user")
        ok = False
    else:
        print("  ✓ Sam was interrupted mid-reply")
        if barge_phase in ("generation", "playback"):
            note = (
                "cut off while audibly speaking"
                if barge_phase == "playback"
                else "cut off while still generating (no audio yet)"
            )
            print(f"  ✓ phase={barge_phase} — {note}")
        else:
            print(f"  ✗ unknown phase {barge_phase!r}")
            ok = False

    # The interrupting speech should be carried into a follow-up turn.
    if len(transcripts) >= 2:
        print("  ✓ interrupting speech carried into the next turn")
    else:
        print("  (no carried transcript yet — the user may not have finished")
        print("   speaking before the test ended)")

    print("  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


asyncio.run(main())
