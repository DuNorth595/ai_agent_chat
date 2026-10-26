#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""
kokoro_tts.py — Text-to-speech via Kokoro-82M.

Lazily loads the model on first call. ~7s cold start, then fast.
Returns WAV bytes (24kHz mono PCM) ready to stream or send.

The voice can be overridden at runtime via the KOKORO_VOICE env var.
Other options: af_heart, bf_emma, am_michael, etc.
"""
import io
import logging
import os
import tempfile
import threading
import time

import numpy as np

import natural_speech
import soundfile as sf
from kokoro import KPipeline

log = logging.getLogger("ava.tts.kokoro")

_pipeline = None
_lock = threading.Lock()

# Fallback chain when env var unset: female voices first (Sam's default),
# then male. Read at call time so live .env edits take effect after restart.
_DEFAULT_FEMALE_VOICES = ("af_heart", "af_bella", "af_sky")
DEFAULT_VOICE = _DEFAULT_FEMALE_VOICES[0]


def _resolve_voice(explicit: str | None = None) -> str:
    """Resolve voice: explicit arg > KOKORO_VOICE env > DEFAULT_VOICE."""
    if explicit:
        return explicit
    env_voice = os.environ.get("KOKORO_VOICE") or os.environ.get("AVA_TTS_VOICE")
    if env_voice:
        return env_voice
    return DEFAULT_VOICE


def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        with _lock:
            if _pipeline is None:
                log.info("loading Kokoro KPipeline (lang_code='a')...")
                t0 = time.time()
                _pipeline = KPipeline(lang_code='a')
                log.info("Kokoro loaded in %.1fs", time.time() - t0)
    return _pipeline


def synth_to_wav_bytes(text: str, voice: str | None = None, speed: float = 1.0,
                       natural: bool | None = None) -> bytes:
    """Synthesize text -> WAV bytes (24kHz mono PCM). Voice resolved at call time.

    When the natural-speech path is enabled, the chunk's leading and
    trailing silence is trimmed before the WAV is written. Kokoro pads
    every chunk with roughly 0.35s of silence at the head and 0.45s at
    the tail; because the bridge synthesizes one sentence at a time,
    those edges land *between* sentences and make the reply sound
    padded and robotic. See natural_speech.py for the measurements.
    """
    voice = _resolve_voice(voice)
    if not text or not text.strip():
        return b""
    if natural is None:
        natural = natural_speech.natural_speech_enabled()
    pipeline = _get_pipeline()
    t0 = time.time()
    chunks = []
    for _, _, audio in pipeline(text, voice=voice, speed=speed):
        chunks.append(audio)
    if not chunks:
        log.warning("Kokoro produced no chunks for text: %r", text[:60])
        return b""
    audio_concat = np.concatenate(chunks)
    if natural:
        audio_concat = natural_speech.trim_silence(audio_concat, sample_rate=24000)
    buf = io.BytesIO()
    sf.write(buf, audio_concat, 24000, format='WAV', subtype='PCM_16')
    buf.seek(0)
    wav_bytes = buf.read()
    log.info(
        "tts: %.2fs audio for %d chars in %.2fs (voice=%s natural=%s)",
        len(audio_concat) / 24000, len(text), time.time() - t0, voice, bool(natural),
    )
    return wav_bytes


def synth_to_pcm_bytes(text: str, voice: str = DEFAULT_VOICE, speed: float = 1.0,
                       sample_rate: int = 24000) -> bytes:
    """Synthesize text → raw PCM s16le mono bytes (no WAV header)."""
    if not text or not text.strip():
        return b""
    pipeline = _get_pipeline()
    t0 = time.time()
    chunks = []
    for _, _, audio in pipeline(text, voice=voice, speed=speed):
        chunks.append(audio)
    if not chunks:
        return b""
    audio_concat = np.concatenate(chunks)
    # Float32 → int16
    pcm = (audio_concat * 32767).astype(np.int16).tobytes()
    log.info(
        "tts-pcm: %d bytes (%d samples, %.2fs) in %.2fs",
        len(pcm), len(audio_concat), len(audio_concat) / sample_rate, time.time() - t0,
    )
    return pcm


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    print("=== testing kokoro_tts.synth_to_wav_bytes ===")
    wav = synth_to_wav_bytes("Hello. This is a voice synthesis test. If you can hear this, it works.")
    print(f"  produced {len(wav)} bytes of WAV")
    assert len(wav) > 1000
    # Save to a temp file (auto-cleaned at process exit) — never write to /tmp/...
    # with a project-specific name; use tempfile so multi-instance runs don't collide.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, prefix="ava-kokoro-test-") as tf:
        tf.write(wav)
        tmp_path = tf.name
    print(f"  saved to {tmp_path}")
    print("  PASS")
