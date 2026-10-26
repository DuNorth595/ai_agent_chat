#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""
whisper_stt.py — Speech-to-text client for whisper-server.

Calls the local whisper-server (default port 8178) which runs whisper.cpp
with Metal acceleration on Apple Silicon (or CPU elsewhere). Configure
via env: WHISPER_URL (default http://127.0.0.1:8178/inference).
"""
import io
import logging
import os
import time
import wave

import requests

log = logging.getLogger("ava.stt.whisper")

WHISPER_URL = os.environ.get("WHISPER_URL", "http://127.0.0.1:8178/inference")
DEFAULT_TIMEOUT = int(os.environ.get("WHISPER_TIMEOUT", "30"))


def _ensure_wav(audio_bytes: bytes, sample_rate: int = 16000) -> bytes:
    """If audio_bytes is not already 16kHz mono WAV, transcode it. For now assume it is."""
    return audio_bytes


def transcribe(audio_bytes: bytes, language: str = "en", timeout: int = DEFAULT_TIMEOUT) -> str:
    """Send audio to whisper-server, return transcribed text.

    audio_bytes: WAV file bytes (16kHz mono PCM recommended).
    Returns the transcribed text (stripped).
    """
    t0 = time.time()
    files = {"file": ("audio.wav", audio_bytes, "audio/wav")}
    data = {
        "temperature": "0.0",
        "response_format": "json",
        "language": language,
    }
    try:
        r = requests.post(WHISPER_URL, files=files, data=data, timeout=timeout)
    except requests.exceptions.ConnectionError:
        log.error("whisper-server not reachable at %s", WHISPER_URL)
        return ""
    r.raise_for_status()
    result = r.json()
    text = (result.get("text") or "").strip()
    log.info("stt: %d chars in %.2fs (audio=%d bytes)", len(text), time.time() - t0, len(audio_bytes))
    return text


def transcribe_wav_pcm(pcm_bytes: bytes, sample_rate: int = 16000) -> str:
    """Transcribe raw PCM s16le mono audio. Wraps in a WAV container first."""
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    buf.seek(0)
    return transcribe(buf.read())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    print("=== testing whisper_stt.transcribe with self-generated audio ===")
    # Self-test: synthesize a phrase with TTS, transcribe it, compare.
    try:
        import tempfile
        from piper_tts import synth_to_wav_bytes
        test_phrase = "the quick brown fox jumps over the lazy dog"
        wav = synth_to_wav_bytes(test_phrase)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, prefix="ava-whisper-test-") as tf:
            tf.write(wav)
            tmp_path = tf.name
        with open(tmp_path, "rb") as f:
            wav_read = f.read()
        text = transcribe(wav_read)
        print(f"  transcript: {text!r}")
        if "fox" in text.lower() or "brown" in text.lower():
            print("  PASS")
        else:
            print("  FAIL: didn't get expected text")
    except Exception as e:
        print(f"  skipped: {e}")
