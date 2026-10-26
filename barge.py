# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""
barge.py — barge-in (full-duplex) voice detection for the ai_agent_chat bridge.

This is a port of Hermes' own `_BargeDetector` (tools/voice_mode.py),
adapted for the widget's transport: the mic is on the PHONE, so audio
arrives as PCM16 frames over the WebSocket rather than from a local
sounddevice InputStream. The detector itself is transport-agnostic — it
consumes (rms, playing) tuples — so the port keeps the exact state
machine and constants.

Why port instead of import: this is a standalone repository. Importing
from another agent framework's source tree would couple a public repo
to a private, machine-specific path. The port is small and
self-contained, and the upstream source is credited here.

Design notes
------------
* The detector's job is to answer one question: "did the user start
  talking while Sam was talking or thinking?" — and if so, WHICH PHASE
  (generation vs playback) so the caller knows whether to abort the brain
  stream, the TTS, or both.

* Pre-roll: when a trip happens we want the audio that came BEFORE the
  trip, otherwise the first syllable is clipped. The caller feeds
  30ms blocks; we keep a ring buffer of the last N blocks and hand it
  back with the trip so the caller can prepend it to the captured
  utterance.

* Calibration: the quiet floor is learned from the first blocks of a
  turn, BEFORE any TTS exists. This matters on the widget because the
  phone's mic picks up Sam's own speaker output — if we calibrated
  during playback we'd learn the echo as "quiet" and never trip.

Constants are copied verbatim from Hermes so the two implementations
behave identically. Change them here only if you also change them there.
"""
from __future__ import annotations

import logging
import math
import os
import sys
from collections import deque
from typing import Optional

log = logging.getLogger("ava.barge")

# ── Constants (verbatim from Hermes tools/voice_mode.py) ──────────────
SAMPLE_RATE = 16000          # Whisper native rate
SILENCE_RMS_THRESHOLD = 200  # RMS below this = silence (int16 range 0-32767)
PLAYBACK_MIN_TRIGGER = 1500.0    # min trigger while TTS flows: bleed alone never trips
TRIGGER_CEILING = 4000.0        # a noisy room must never push the trigger past normal speech
DEFAULT_BARGE_MULTIPLIER = 3.0  # over the quiet floor: 300*3=900 vs 3000+ speech

# Block size for the ring buffer / pre-roll. 30ms at 16kHz mono s16le.
BLOCK_MS = 30
BLOCK_SAMPLES = SAMPLE_RATE * BLOCK_MS // 1000  # 480 samples
BLOCK_BYTES = BLOCK_SAMPLES * 2                   # int16


def _vad_log(msg: str) -> None:
    """VAD diagnostic: logger.debug, plus stderr when AVA_BARGE_DEBUG=1."""
    log.debug(msg)
    if os.environ.get("AVA_BARGE_DEBUG", "").strip() == "1":
        try:
            print(f"[ava-barge] {msg}", file=sys.stderr, flush=True)
        except Exception:
            pass


def rms_int16(pcm: bytes) -> float:
    """RMS of a PCM16 mono block, matching Hermes' _rms() on int16 input.

    Hermes computes: sqrt(mean(x.astype(float64)**2)) over int16 samples.
    We replicate that exactly so thresholds transfer 1:1.
    """
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    total = 0.0
    # struct-free manual loop over int16 LE via int.from_bytes pairs is slow;
    # use array module which is C-speed.
    import array
    a = array.array("h")
    a.frombytes(pcm[: n * 2])
    if sys.byteorder == "big":
        a.byteswap()
    for v in a:
        total += float(v) * float(v)
    return math.sqrt(total / n)


class BargeDetector:
    """Per-block barge-in state machine.

    Feed it (rms, playing) per 30ms block. It returns a phase name
    ("generation" | "playback") when speech trips, else None.

    Ported from Hermes `tools/voice_mode.py::_BargeDetector`. The state
    machine, constants and grace logic are unchanged.
    """

    def __init__(
        self,
        *,
        mult: float = DEFAULT_BARGE_MULTIPLIER,
        calib_blocks: int = 20,
        trip_blocks: int = 5,
        grace_blocks: int = 4,
    ) -> None:
        self.mult = float(mult)
        self.calib_blocks = int(calib_blocks)
        self.grace_blocks = int(grace_blocks)
        self.trip_needed = max(1, round(trip_blocks * 0.8))
        self.ambient: deque = deque(maxlen=100)  # ~3s of quiet-phase RMS
        self.recent_above: deque = deque(maxlen=trip_blocks)
        self.quiet_floor = float(SILENCE_RMS_THRESHOLD)
        self.floor_locked = False
        self.playing_prev = False
        self.playback_seen = False
        self.grace_remaining = 0
        self.block_idx = 0
        self.blocks_since_playback = 10_000

    def _floor(self) -> tuple:
        """(pct90, floor): 90th percentile of the quiet window; floor never
        below the silence threshold."""
        pct90 = (
            float(sorted(self.ambient)[int(0.9 * (len(self.ambient) - 1))])
            if self.ambient
            else float(SILENCE_RMS_THRESHOLD)
        )
        return pct90, max(pct90, float(SILENCE_RMS_THRESHOLD))

    def _calibrate(self, rms: float, playing: bool) -> bool:
        """Lock the floor from the first calib_blocks: the listener arms at
        utterance start, before any TTS exists, so they sample the quiet
        room — NOT bleed."""
        if not playing:
            self.ambient.append(rms)
        if len(self.ambient) >= self.calib_blocks or playing:
            pct90, self.quiet_floor = self._floor()
            self.floor_locked = True
            _vad_log(
                f"calibrated quiet floor={self.quiet_floor:.0f} "
                f"(pct90={pct90:.0f}, {len(self.ambient)} blocks, mult={self.mult:g})"
            )
        return self.floor_locked

    def _track_playback(self, playing: bool) -> None:
        """Grace only when playback starts after a real gap (>=1s), so
        inter-sentence flapping of the audio-active flag can't chain
        windows and swallow an interjection."""
        if playing and not self.playing_prev:
            if not self.playback_seen or self.blocks_since_playback > 33:
                self.grace_remaining = self.grace_blocks
                _vad_log(
                    f"playback started (block={self.block_idx}) — "
                    f"grace {self.grace_blocks * BLOCK_MS}ms"
                )
            self.playback_seen = True
        self.playing_prev = playing
        self.blocks_since_playback = (
            0 if playing else self.blocks_since_playback + 1
        )

    def feed(self, rms: float, playing: bool) -> Optional[str]:
        """Consume one block; return the phase name when speech trips."""
        self.block_idx += 1
        if not self.floor_locked and not self._calibrate(rms, playing):
            return None
        self._track_playback(playing)
        # Trigger: quiet baseline x multiplier, phase-clamped.
        trigger = max(
            self.quiet_floor * self.mult,
            PLAYBACK_MIN_TRIGGER if playing else float(SILENCE_RMS_THRESHOLD) * 2,
        )
        trigger = min(trigger, TRIGGER_CEILING)
        # Track ambient drift ONLY while nothing plays (never absorb bleed).
        if not playing and rms < trigger:
            self.ambient.append(rms)
            _, self.quiet_floor = self._floor()
        above = rms >= trigger
        if above and self.grace_remaining > 0:
            _vad_log(
                f"grace suppression: block={self.block_idx} rms={rms:.0f} "
                f"trigger={trigger:.0f} ({self.grace_remaining} blocks left)"
            )
            above = False
        if self.grace_remaining > 0:
            self.grace_remaining -= 1
        self.recent_above.append(above)
        phase = "playback" if playing else "generation"
        if rms >= trigger * 0.5:
            _vad_log(
                f"block={self.block_idx} rms={rms:.0f} "
                f"floor={self.quiet_floor:.0f} trigger={trigger:.0f} "
                f"above={above} window={sum(self.recent_above)}/{self.trip_needed} "
                f"phase={phase}"
            )
        if not (above and sum(self.recent_above) >= self.trip_needed):
            return None
        _vad_log(
            f"TRIPPED ({phase}): block={self.block_idx} rms={rms:.0f} "
            f"floor={self.quiet_floor:.0f} trigger={trigger:.0f} "
            f"window={sum(self.recent_above)}/{len(self.recent_above)}"
        )
        return phase


class PreRollBuffer:
    """Ring buffer of recent PCM blocks, so the first syllable of an
    interrupted utterance isn't clipped.

    Feed every block. On trip, `take()` returns the buffered audio
    (pre-roll) so the caller can prepend it to the captured utterance.
    """

    def __init__(self, max_blocks: int = 40) -> None:
        self.max_blocks = int(max_blocks)
        self._buf: deque = deque(maxlen=self.max_blocks)
        self._bytes = 0

    def push(self, pcm: bytes) -> None:
        self._buf.append(pcm)
        self._bytes += len(pcm)

    def take(self) -> bytes:
        """Return and clear the buffered pre-roll."""
        if not self._buf:
            return b""
        out = b"".join(self._buf)
        self._buf.clear()
        self._bytes = 0
        return out

    def peek_len(self) -> int:
        return self._bytes


def frames_from_pcm(pcm: bytes) -> list[bytes]:
    """Split a PCM16 mono byte string into BLOCK_BYTES-sized frames.

    Any trailing partial frame is dropped (we need whole 30ms blocks for
    the RMS math to be stable).
    """
    if not pcm:
        return []
    n = len(pcm) // BLOCK_BYTES
    return [pcm[i * BLOCK_BYTES:(i + 1) * BLOCK_BYTES] for i in range(n)]


def pcm_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Wrap raw PCM16 mono in a WAV header (for whisper.cpp, which is
    WAV-only)."""
    import struct
    data_len = len(pcm)
    hdr = b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVE"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                                sample_rate * 2, 2, 16)
    hdr += b"data" + struct.pack("<I", data_len)
    return hdr + pcm


__all__ = [
    "BargeDetector",
    "PreRollBuffer",
    "rms_int16",
    "frames_from_pcm",
    "pcm_to_wav",
    "SAMPLE_RATE",
    "BLOCK_MS",
    "BLOCK_BYTES",
    "SILENCE_RMS_THRESHOLD",
    "PLAYBACK_MIN_TRIGGER",
    "TRIGGER_CEILING",
    "DEFAULT_BARGE_MULTIPLIER",
]
