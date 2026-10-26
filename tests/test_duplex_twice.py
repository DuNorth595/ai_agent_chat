# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""Regression test for two duplex bugs reported during development.

BUG 1: "after you reply, your mic doesn't release and I can't talk again"
BUG 2: "you get partway into the reply and it restarts, so you end up
        repeating part of it"

This test drives the duplex protocol over the WS the way the phone
does, twice in a row, and checks:
  * the server accepts a SECOND utterance after the first reply
    (bug 1 — the client-side mic tap must stay live)
  * the reply text contains no duplicated prefix (bug 2 — the brain
    must not re-loop on max_tokens)

Run: python tests/test_duplex_twice.py   (or: source .venv/bin/activate)
"""
import asyncio
import base64
import json
import math
import os
import re
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


def frames(pcm):
    step = FRAME_SAMPLES * 2
    return [pcm[i:i + step] for i in range(0, len(pcm) - step + 1, step)]


def dup_prefix_len(a: str, b: str) -> int:
    """Length of the longest common prefix (character-level) of two strings."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


async def one_utterance(ws, phrase, wav_path, label):
    """Speak one phrase over the socket; return the reply text."""
    pcm = synth(phrase, wav_path)
    fr = frames(pcm)

    # Calibrate with silence.
    for _ in range(25):
        await ws.send(json.dumps({
            "type": "pcm", "data_b64": base64.b64encode(silence()).decode(),
        }))
        await asyncio.sleep(0.005)

    # Speak.
    for f in fr:
        await ws.send(json.dumps({
            "type": "pcm", "data_b64": base64.b64encode(f).decode(),
        }))
        await asyncio.sleep(0.030)

    # A beat of silence so the endpoint is clean.
    for _ in range(12):
        await ws.send(json.dumps({
            "type": "pcm", "data_b64": base64.b64encode(silence()).decode(),
        }))
        await asyncio.sleep(0.01)

    await ws.send(json.dumps({"type": "barge_stop"}))
    print(f"  [{label}] spoke: {phrase!r}")


async def main():
    require_piper()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    turns = []          # list of (transcript, reply_text)
    cur = {"t": None, "text": []}

    def _reset():
        return {"t": None, "text": []}

    # max_size=None: the server ships multi-hundred-KB base64 WAV frames
    # and the default 1MB limit drops the connection mid-reply.
    # ping_interval=None: the `websockets` library auto-pings and closes
    # the connection if the server doesn't PONG within ping_timeout. The
    # server is busy synthesizing TTS for long replies and can't answer
    # pings, so the test client dropped the connection mid-reply. Real
    # browsers do NOT auto-ping, so this is a test-only artifact.
    async with websockets.connect(
        WS_URL, ssl=ctx, max_size=None, ping_interval=None
    ) as ws:
        await ws.send(json.dumps({"type": "duplex", "session_id": "twice-1"}))
        print("  sent {type: duplex}")

        done = asyncio.Event()

        async def reader():
            while True:
                try:
                    raw = await ws.recv()
                except Exception:
                    done.set()
                    return
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "transcript":
                    cur["t"] = msg.get("final")
                elif t == "text":
                    cur["text"].append(msg.get("text", ""))
                elif t == "done":
                    turns.append((cur["t"], "".join(cur["text"])))
                    cur.clear()
                    cur.update(_reset())
                    done.set()
                    return

        # The reader has to run concurrently with the speaking — it is
        # what collects the server's replies.
        rtask = asyncio.get_event_loop().create_task(reader())

        # ── TURN 1: a question that produces a long answer ──
        await one_utterance(
            ws,
            "In three sentences, what was the Roman Republic?",
            "/tmp/duplex_long.wav",
            "turn 1",
        )
        try:
            await asyncio.wait_for(done.wait(), timeout=400)
        except asyncio.TimeoutError:
            print("  FAIL: turn 1 never completed")
            return
        print(f"  [turn 1] reply: {len(turns[0][1])} chars")

        # ── TURN 2: the mic must still work ──
        # The reader returns after the first 'done', so start a new one.
        rtask2 = asyncio.get_event_loop().create_task(reader())
        done.clear()
        await one_utterance(
            ws,
            "And in two sentences, who was its first dictator?",
            "/tmp/duplex_long2.wav",
            "turn 2",
        )
        try:
            await asyncio.wait_for(done.wait(), timeout=400)
        except asyncio.TimeoutError:
            print("  FAIL: turn 2 never completed — THE MIC IS DEAD (bug 1)")
            return
        print(f"  [turn 2] reply: {len(turns[1][1])} chars")

    print()
    print("=" * 66)
    ok = True
    for i, (heard, reply) in enumerate(turns, 1):
        print(f"  turn {i} heard : {heard!r}")
    if len(turns) < 2:
        print("  BUG 1 (mic dead after first reply): NOT REPRODUCED / STILL PRESENT")
        ok = False
    else:
        print("  BUG 1 (mic dead after first reply): FIXED — second turn answered")

    # Bug 2: the reply must not restart. Check the turn-1 reply for a
    # duplicated opening.
    if turns:
        reply = turns[0][1]
        # A restarted reply shows the same opening sentence twice.
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", reply) if s.strip()]
        seen = {}
        for s in sentences:
            seen[s] = seen.get(s, 0) + 1
        dupes = {s: c for s, c in seen.items() if c > 1 and len(s) > 12}
        if dupes:
            print(f"  BUG 2 (reply restarts): STILL PRESENT — {len(dupes)} repeated sentence(s)")
            for s, c in list(dupes.items())[:3]:
                print(f"      x{c}: {s[:80]!r}")
            ok = False
        else:
            print("  BUG 2 (reply restarts): FIXED — no repeated sentences")

    print("=" * 66)
    print("  RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


asyncio.run(main())
