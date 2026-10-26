# SPDX-FileCopyrightText: 2026 S_DevLabs
# SPDX-License-Identifier: MIT
"""Tests for the natural-speech path.

Covers three things:

1. The silence trim actually removes dead air without removing speech.
2. The speed jitter is deterministic and stays within bounds.
3. The whole thing is a no-op when AVA_NATURAL_SPEECH is off.

Run:
    .venv-iris/bin/python tests/test_natural_speech.py
"""

import io
import os
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import natural_speech as ns

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def synth(text, natural):
    """Synthesize one sentence and return float32 samples."""
    from dotenv import load_dotenv
    load_dotenv(
        Path(__file__).resolve().parent.parent / ".env", override=False
    )
    import kokoro_tts
    wav = kokoro_tts.synth_to_wav_bytes(text, natural=natural)
    # Skip the 44-byte RIFF header.
    samples = np.frombuffer(wav[44:], dtype=np.int16).astype(np.float32) / 32768.0
    return samples


def test_trim_removes_dead_air():
    print("\n[test] trim removes dead air, keeps speech")
    text = "So, I think we should probably look at that later, right?"
    raw = synth(text, natural=False)
    trimmed = synth(text, natural=True)

    raw_stats = ns.measure_silence(raw)
    trim_stats = ns.measure_silence(trimmed)

    print(f"      raw    {raw_stats['duration']:.2f}s silence={raw_stats['silence']:.2f}s")
    print(f"      trim   {trim_stats['duration']:.2f}s silence={trim_stats['silence']:.2f}s")

    check("trimmed is shorter", trim_stats["duration"] < raw_stats["duration"],
          f"{trim_stats['duration']:.2f} vs {raw_stats['duration']:.2f}")
    check("trimmed has less silence",
          trim_stats["silence"] < raw_stats["silence"],
          f"{trim_stats['silence']:.2f} vs {raw_stats['silence']:.2f}")
    # The speech itself must survive: energy content should be close.
    check("speech energy preserved",
          trim_stats["rms"] > raw_stats["rms"] * 0.6,
          f"rms {trim_stats['rms']:.4f} vs {raw_stats['rms']:.4f}")
    check("trimmed audio is not empty", len(trimmed) > 1000)


def test_trim_preserves_interior_pauses():
    print("\n[test] trim only touches the edges")
    # A long sentence with an internal comma pause.
    text = "I went to the store yesterday, and I bought some milk."
    raw = synth(text, natural=False)
    trimmed = synth(text, natural=True)

    raw_stats = ns.measure_silence(raw)
    trim_stats = ns.measure_silence(trimmed)
    # Interior pauses should mostly survive; only edges get cut.
    check("interior pause survives",
          trim_stats["silence"] > 0.05,
          f"silence {trim_stats['silence']:.3f}s")
    check("only edges removed",
          trim_stats["silence"] >= raw_stats["silence"] * 0.3,
          f"{trim_stats['silence']:.3f} vs {raw_stats['silence']:.3f}")


def test_jitter_is_deterministic_and_bounded():
    print("\n[test] speed jitter is deterministic and bounded")
    sentence = "I was thinking about that thing we talked about yesterday."
    first = ns.sentence_speed(sentence)
    second = ns.sentence_speed(sentence)
    check("same sentence -> same speed", abs(first - second) < 1e-9,
          f"{first} vs {second}")

    speeds = [ns.sentence_speed(s) for s in [
        "I was thinking about that thing we talked about yesterday.",
        "Yeah, that sounds like a good plan to me honestly.",
        "We should probably look at the logs before changing anything.",
        "Alright, I will take care of that in the morning.",
        "Let me check whether the service is actually running first.",
    ]]
    check("different sentences vary", len(set(speeds)) > 1, str(speeds))
    check("all within jitter bound",
          all(abs(s - 1.0) <= ns.SPEED_JITTER + 1e-9 for s in speeds),
          str(speeds))

    # Short fragments stay at 1.0 -- jitter there sounds like a stumble.
    check("short fragment unjittered", ns.sentence_speed("Yeah.") == 1.0)
    check("empty unjittered", ns.sentence_speed("") == 1.0)


def test_piper_speed_is_inverted():
    print("\n[test] piper speed inverts kokoro speed")
    sentence = "I was thinking about that thing we talked about yesterday."
    kok = ns.kokoro_speed(sentence)
    pip = ns.piper_speed(sentence)
    # Piper's speed is --length-scale: higher means slower. So the two
    # must move in opposite directions around 1.0.
    if kok > 1.0:
        check("piper inverts kokoro", pip < 1.0, f"kok={kok} piper={pip}")
    elif kok < 1.0:
        check("piper inverts kokoro", pip > 1.0, f"kok={kok} piper={pip}")
    else:
        check("piper inverts kokoro", abs(pip - 1.0) < 1e-9)


def test_sentence_split_keeps_punctuation():
    print("\n[test] sentence split preserves terminal punctuation")
    parts = ns.split_sentences(
        "I was thinking about that. What did you say? Alright, let's do it! "
    )
    check("three sentences", len(parts) == 3, str(parts))
    check("period kept", parts[0].endswith("."), parts[0])
    check("question kept", parts[1].endswith("?"), parts[1])
    check("exclamation kept", parts[2].endswith("!"), parts[2])


def test_feature_flag_gates_everything():
    print("\n[test] feature flag gates the path")
    os.environ.pop("AVA_NATURAL_SPEECH", None)
    check("off by default", ns.natural_speech_enabled() is False)
    os.environ["AVA_NATURAL_SPEECH"] = "1"
    check("on when set to 1", ns.natural_speech_enabled() is True)
    os.environ["AVA_NATURAL_SPEECH"] = "0"
    check("off when set to 0", ns.natural_speech_enabled() is False)
    os.environ.pop("AVA_NATURAL_SPEECH", None)


def test_naturalize_segments():
    print("\n[test] naturalize_segments joins with a flat gap")
    tone_a = np.concatenate([
        np.zeros(1000, dtype=np.float32),
        np.ones(2400, dtype=np.float32) * 0.5,
        np.zeros(2000, dtype=np.float32),
    ])
    tone_b = np.concatenate([
        np.zeros(1000, dtype=np.float32),
        np.ones(2400, dtype=np.float32) * 0.5,
        np.zeros(2000, dtype=np.float32),
    ])
    joined = ns.naturalize_segments([tone_a, tone_b], gap_ms=150)
    stats = ns.measure_silence(joined)
    # Two 100ms-ish tones + a 150ms gap, minus trimmed edges.
    check("gap present between segments", stats["max_gap"] >= 0.14,
          f"max_gap={stats['max_gap']:.3f}s")
    check("no trailing gap after last",
          stats["duration"] < len(tone_a) + len(tone_b),
          f"{stats['duration']:.3f}s")


def main():
    print("=" * 60)
    print("natural-speech tests")
    print("=" * 60)
    test_feature_flag_gates_everything()
    test_jitter_is_deterministic_and_bounded()
    test_piper_speed_is_inverted()
    test_sentence_split_keeps_punctuation()
    test_naturalize_segments()
    test_trim_removes_dead_air()
    test_trim_preserves_interior_pauses()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
