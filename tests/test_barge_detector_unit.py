# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""Standalone test of barge.py — verifies the detector trips on speech and
not on silence/noise, and that the RMS math matches Hermes' _rms().

Run: python tests/test_barge_detector_unit.py
"""
import math
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import barge


def make_tone(amplitude, n_samples, sr=16000, freq=440):
    """16kHz mono s16le sine at given amplitude."""
    out = bytearray()
    for i in range(n_samples):
        v = int(amplitude * math.sin(2 * math.pi * freq * i / sr))
        out += struct.pack("<h", v)
    return bytes(out)


def rms_ref(pcm):
    """Hermes' formula: sqrt(mean(x.astype(float64)**2)) on int16."""
    n = len(pcm) // 2
    vals = struct.unpack("<%dh" % n, pcm[: n * 2])
    return math.sqrt(sum(float(v) * float(v) for v in vals) / n)


print("=" * 62)
print("1. RMS matches Hermes _rms() reference")
for amp, label in [(0, "silence"), (300, "quiet"), (6000, "speech")]:
    pcm = make_tone(amp, 480)
    mine = barge.rms_int16(pcm)
    ref = rms_ref(pcm)
    ok = abs(mine - ref) < 1e-6
    print(f"   {label:8} amp={amp:6}  mine={mine:9.3f}  ref={ref:9.3f}  "
          f"{'OK' if ok else 'MISMATCH'}")

print()
print("=" * 62)
print("2. Detector: silence never trips")
det = barge.BargeDetector()
silence = make_tone(0, barge.BLOCK_SAMPLES)
trip = None
for i in range(60):
    trip = det.feed(barge.rms_int16(silence), playing=False)
    if trip:
        break
print(f"   fed 60 silent blocks, tripped: {trip}  "
      f"{'OK' if trip is None else 'FAIL'}")
print(f"   quiet_floor locked = {det.floor_locked}, floor={det.quiet_floor:.0f}")

print()
print("=" * 62)
print("3. Detector: speech during GENERATION trips as 'generation'")
det = barge.BargeDetector()
# 30 quiet blocks to calibrate
for i in range(30):
    det.feed(barge.rms_int16(silence), playing=False)
print(f"   calibrated floor = {det.quiet_floor:.0f}")
# now speech
speech = make_tone(7000, barge.BLOCK_SAMPLES)
trip = None
for i in range(20):
    trip = det.feed(barge.rms_int16(speech), playing=False)
    if trip:
        break
print(f"   tripped: {trip!r}  {'OK' if trip == 'generation' else 'FAIL'}")

print()
print("=" * 62)
print("4. Detector: speech during PLAYBACK trips as 'playback'")
det = barge.BargeDetector()
for i in range(30):
    det.feed(barge.rms_int16(silence), playing=False)
# start playback
trip = None
for i in range(30):
    trip = det.feed(barge.rms_int16(speech), playing=True)
    if trip:
        break
print(f"   tripped: {trip!r}  {'OK' if trip == 'playback' else 'FAIL'}")

print()
print("=" * 62)
print("5. Detector: playback BLEED (quiet, 600) does NOT trip")
det = barge.BargeDetector()
for i in range(30):
    det.feed(barge.rms_int16(silence), playing=False)
bleed = make_tone(600, barge.BLOCK_SAMPLES)
trip = None
for i in range(60):
    trip = det.feed(barge.rms_int16(bleed), playing=True)
    if trip:
        break
print(f"   tripped: {trip!r}  {'OK' if trip is None else 'FAIL (bleed tripped!)'}")

print()
print("=" * 62)
print("6. Pre-roll buffer returns buffered audio then clears")
buf = barge.PreRollBuffer(max_blocks=5)
for i in range(10):
    buf.push(bytes([i]) * barge.BLOCK_BYTES)
got = buf.take()
print(f"   expected 5 blocks = {5 * barge.BLOCK_BYTES} bytes, got {len(got)}  "
      f"{'OK' if len(got) == 5 * barge.BLOCK_BYTES else 'FAIL'}")
print(f"   second take() = {len(buf.take())} bytes (expect 0)  "
      f"{'OK' if len(buf.take()) == 0 else 'FAIL'}")

print()
print("=" * 62)
print("7. frames_from_pcm splits correctly")
pcm = make_tone(1000, 480 * 7)  # 7 frames
frames = barge.frames_from_pcm(pcm)
print(f"   7 blocks -> {len(frames)} frames  "
      f"{'OK' if len(frames) == 7 else 'FAIL'}")
print(f"   each frame = {len(frames[0])} bytes (expect {barge.BLOCK_BYTES})")

print()
print("=" * 62)
print("8. pcm_to_wav produces a valid RIFF header")
wav = barge.pcm_to_wav(make_tone(1000, 480))
print(f"   magic = {wav[:4]!r} {wav[8:12]!r}  "
      f"{'OK' if wav[:4] == b'RIFF' and wav[8:12] == b'WAVE' else 'FAIL'}")
print(f"   total len = {len(wav)} (expect 44 + 960)  "
      f"{'OK' if len(wav) == 44 + 960 else 'FAIL'}")
