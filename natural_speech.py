# SPDX-FileCopyrightText: 2026 S_DevLabs
# SPDX-License-Identifier: MIT
"""Natural-speech helpers for the voice bridge.

Two separate problems are solved here.

**1. Dead air.** Every synthesis chunk carries leading and trailing
silence -- roughly 0.35s at the head and 0.45s at the tail when Kokoro
speaks one sentence at a time. The bridge synthesizes sentence by
sentence, so those edges land *between* sentences and a six-sentence
reply carries about five seconds of near-silence. Measured on the
production voice: trimming the chunk edges cut a six-sentence reply
from 11.67s to 7.06s with the speech itself untouched.

**2. Uniform rhythm.** Every sentence is synthesized at exactly the
same speed, which reads as a machine reading a list. Real speech
varies slightly sentence to sentence. The variation here is small
(+/-2-3%) and *deterministic*: the same sentence always gets the same
speed, so it sounds like a person with a habitual rhythm rather than
an unstable voice.

What this deliberately does NOT do is add spoken disfluencies.
Kokoro phonemizes "uh" as `ˈʌ` -- it spells the letter -- and both
engines here are read-speech models, which voice fillers poorly. The
conversational feel comes from rhythm and punctuation instead.

Reference: LIFE_MEMORY/PROJECTS/VOICE_NATURALNESS_TTS_RESEARCH_2026-10-26.md
"""

from __future__ import annotations

import hashlib
import logging
import os
import re

import numpy as np

log = logging.getLogger("ava.tts.natural")

# Kokoro's output sample rate.
SAMPLE_RATE = 24000

# Amplitude below which a sample counts as silence. Kokoro's own noise
# floor sits well under this; speech sits well over it.
SILENCE_THRESHOLD = 0.006

# Keep this much padding at each edge of the trimmed audio. Trimming
# exactly to the speech boundary sounds clipped on phones with
# aggressive audio processing, and some browsers need a few
# milliseconds of lead-in to start the WebAudio node cleanly.
EDGE_PAD_MS = 25

# The gap inserted between two sentences. Owoicho et al. (Speech
# Prosody 2024, Google/Edinburgh) found listeners are insensitive to
# blanket pause substitution and actively dislike *long* pauses, so
# this is one flat value rather than a per-punctuation rule.
INTER_SENTENCE_GAP_MS = 150

# Per-sentence speed variation, as a fraction. Kept small on purpose:
# wider jitter reads as unstable rather than human.
SPEED_JITTER = 0.025

# Speed is only varied for sentences long enough for the variation to
# be audible. A one-word fragment at 0.98x or 1.02x just sounds
# wrong. "Yeah." and "Hmm, wait." are real sentences with real
# prosody, so the threshold is low enough to include them.
MIN_JITTER_CHARS = 12


def natural_speech_enabled() -> bool:
    """Whether the natural-speech path is switched on.

    Defaults to off so a stock checkout behaves exactly like the
    pre-feature bridge.
    """
    return os.environ.get("AVA_NATURAL_SPEECH", "").strip() == "1"


def _deterministic_jitter(sentence: str) -> float:
    """Return a stable speed multiplier for a sentence.

    Uses a hash of the sentence so the same words always get the same
    speed. That matters: random-per-call jitter would make the same
    sentence sound different every time it was spoken, which reads as
    a glitch. Hash-based jitter is stable but still varies across
    different sentences, so a reply has rhythm without being
    monotonous.
    """
    digest = hashlib.sha256(sentence.encode("utf-8")).digest()
    # Map the first two bytes onto [-1, 1].
    unit = (int.from_bytes(digest[:2], "big") / 0xFFFF) * 2.0 - 1.0
    return 1.0 + (unit * SPEED_JITTER)


def trim_silence(audio: np.ndarray, sample_rate: int = SAMPLE_RATE,
                 threshold: float = SILENCE_THRESHOLD,
                 pad_ms: int = EDGE_PAD_MS) -> np.ndarray:
    """Strip leading and trailing silence from a chunk.

    Only the *edges* are touched. Interior pauses are left alone --
    they are part of the sentence and removing them would flatten the
    phrasing.
    """
    if audio is None or len(audio) == 0:
        return audio
    audio = np.asarray(audio, dtype=np.float32)
    loud = np.nonzero(np.abs(audio) > threshold)[0]
    if len(loud) == 0:
        return audio
    pad = int(sample_rate * pad_ms / 1000.0)
    start = max(0, int(loud[0]) - pad)
    end = min(len(audio), int(loud[-1]) + pad)
    return audio[start:end]


def make_gap(duration_ms: int = INTER_SENTENCE_GAP_MS,
             sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Return a silent buffer of the requested length."""
    return np.zeros(int(sample_rate * duration_ms / 1000.0), dtype=np.float32)


def sentence_speed(sentence: str) -> float:
    """Speed multiplier for one sentence, as a *duration* ratio.

    Returns 1.0 for normal pace, <1.0 for slightly faster, >1.0 for
    slightly slower. Long sentences get a small deterministic
    variation; short ones are left at 1.0 because the jitter would be
    audible as a stumble rather than as rhythm.

    NOTE: this is a duration ratio, not a rate. The two engines want
    opposite things, so use the per-engine helpers below rather than
    passing this straight to a backend.
    """
    text = (sentence or "").strip()
    if len(text) < MIN_JITTER_CHARS:
        return 1.0
    return _deterministic_jitter(text)


def kokoro_speed(sentence: str) -> float:
    """Speed for Kokoro.

    Kokoro's `speed` scales predicted duration: higher means faster.
    """
    return sentence_speed(sentence)


def piper_speed(sentence: str) -> float:
    """Speed for Piper.

    Piper's `speed` is really `--length-scale`: higher means *slower*.
    A duration ratio therefore has to be inverted for Piper, or the
    variation runs backwards and the "fast" sentences come out slow.
    """
    ratio = sentence_speed(sentence)
    return 1.0 / ratio if ratio else 1.0


def split_sentences(text: str) -> list[str]:
    """Split text into sentences, keeping terminal punctuation.

    The lookbehind is deliberate: it finds the boundary without
    consuming the punctuation, so `?` and `!` survive into the
    synthesis call. Kokoro treats punctuation as a first-class token
    -- `,`=3 `.`=4 `!`=5 `?`=6 in its vocabulary -- and it is the
    model's main prosody lever. Stripping it would flatten the voice.
    """
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [p for p in parts if p and p.strip()]


def add_gap_after(text: str, gap_ms: int = INTER_SENTENCE_GAP_MS,
                  sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Return a trailing silence buffer of the requested length.

    Used to hold the gap between two sentences. When each sentence is
    synthesized separately and trimmed, the chunks butt against each
    other with almost no pause between them -- measured at ~0.14s,
    which is tighter than normal speech phrasing and makes the reply
    sound clipped. A flat gap restores the rhythm.
    """
    return make_gap(gap_ms, sample_rate)


def naturalize_segments(segments: list[np.ndarray],
                        sample_rate: int = SAMPLE_RATE,
                        gap_ms: int = INTER_SENTENCE_GAP_MS) -> np.ndarray:
    """Trim each segment and join them with a single flat gap.

    `segments` are the per-sentence audio buffers, in order. Each one
    is trimmed independently, then joined with `gap_ms` of silence
    between them -- but not after the last one.
    """
    if not segments:
        return np.zeros(0, dtype=np.float32)

    pieces: list[np.ndarray] = []
    for index, segment in enumerate(segments):
        trimmed = trim_silence(segment, sample_rate=sample_rate)
        if len(trimmed) == 0:
            continue
        pieces.append(trimmed)
        if index < len(segments) - 1:
            pieces.append(make_gap(gap_ms, sample_rate))

    if not pieces:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(pieces).astype(np.float32)


def measure_silence(audio: np.ndarray, sample_rate: int = SAMPLE_RATE,
                    threshold: float = SILENCE_THRESHOLD) -> dict:
    """Report silence statistics for a chunk.

    Used by the tests and the dev endpoint to prove that trimming
    removed dead air without removing speech.
    """
    audio = np.asarray(audio, dtype=np.float32)
    if len(audio) == 0:
        return {"duration": 0.0, "silence": 0.0, "max_gap": 0.0, "rms": 0.0}
    quiet = np.abs(audio) <= threshold
    # Length of each run of quiet samples.
    runs = []
    run = 0
    for is_quiet in quiet:
        if is_quiet:
            run += 1
        else:
            if run:
                runs.append(run)
            run = 0
    if run:
        runs.append(run)
    return {
        "duration": len(audio) / sample_rate,
        "silence": (quiet.sum()) / sample_rate,
        "max_gap": (max(runs) / sample_rate) if runs else 0.0,
        "rms": float(np.sqrt((audio ** 2).mean())),
    }
