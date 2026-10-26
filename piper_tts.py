# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""
piper_tts.py — Piper TTS adapter.

Piper is MIT-licensed, ~70x realtime on CPU, no model download required.
This adapter shells out to a `piper` CLI binary. Configure paths via
environment variables:

    PIPER_BIN           path to the piper executable
                        (default: piper on $PATH)
    PIPER_MODEL_DIR     directory containing *.onnx + *.onnx.json voice files
                        (default: ~/.local/share/piper/voices)
    PIPER_MODEL         voice model filename (without .onnx extension)
                        (default: en_US-libritts_r-medium)

Output: 22050Hz mono s16le WAV bytes by default. Set PIPER_SAMPLE_RATE
to override.
"""
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger("ava.tts.piper")


def _default_model_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "piper" / "voices"


MODEL_DIR = Path(os.environ.get("PIPER_MODEL_DIR") or _default_model_dir())
DEFAULT_MODEL_NAME = os.environ.get("PIPER_MODEL", "en_US-libritts_r-medium")
DEFAULT_MODEL = MODEL_DIR / f"{DEFAULT_MODEL_NAME}.onnx"
DEFAULT_CONFIG = MODEL_DIR / f"{DEFAULT_MODEL_NAME}.onnx.json"

# Path to the piper CLI binary. Default: rely on $PATH; override if you
# have a venv-installed copy (e.g. `python -m piper` or `/path/to/venv/bin/piper`).
PIPER_BIN = os.environ.get("PIPER_BIN", "piper")

# 22050Hz mono s16le — matches the libritts_r model card. Override per voice.
SAMPLE_RATE = int(os.environ.get("PIPER_SAMPLE_RATE", "22050"))


def is_available() -> bool:
    """True if piper CLI + model files are reachable."""
    bin_ok = (
        Path(PIPER_BIN).exists()
        if os.path.sep in PIPER_BIN
        else _which(PIPER_BIN) is not None
    )
    return bin_ok and DEFAULT_MODEL.exists() and DEFAULT_CONFIG.exists()


def _which(bin_name: str) -> str | None:
    """Locate a binary on PATH. Returns absolute path or None."""
    for p in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(p) / bin_name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def synth_to_wav_bytes(text: str, voice: str = None, speed: float = 1.0) -> bytes:
    """
    Synthesize text → WAV bytes via Piper CLI.

    Args:
        text:  what to say
        voice: unused (piper models are single-speaker; kept for API compat with kokoro_tts)
        speed: length scale — 1.0 is normal, lower = faster, higher = slower
               Piper calls it --length-scale (default 1.0).
    """
    if not text or not text.strip():
        return b""
    if not is_available():
        raise FileNotFoundError(
            f"piper unavailable: bin={PIPER_BIN!r} (resolved={bool(_which(PIPER_BIN) or Path(PIPER_BIN).exists())}) "
            f"model={DEFAULT_MODEL.exists()} config={DEFAULT_CONFIG.exists()}"
        )

    t0 = time.time()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_wav = tmp.name
    try:
        # Resolve piper binary. If PIPER_BIN is a path, use it directly;
        # otherwise look it up on PATH.
        bin_path = PIPER_BIN if os.path.sep in PIPER_BIN else _which(PIPER_BIN) or PIPER_BIN
        # --length-scale inverts intuition: 1.0 is default, 0.5 = twice as
        # fast, 2.0 = twice as slow. We invert `speed` so callers can pass
        # the same `speed=1.2` as kokoro_tts and get the same result.
        length_scale = (1.0 / speed) if speed > 0 else 1.0
        proc = subprocess.run(
            [
                bin_path,
                "--model", str(DEFAULT_MODEL),
                "--config", str(DEFAULT_CONFIG),
                "--output_file", tmp_wav,
                "--length-scale", f"{length_scale:.3f}",
                "--noise-scale", "0.333",
                "--noise-w", "0.333",
            ],
            input=text,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace")[:500]
            raise RuntimeError(f"piper failed (rc={proc.returncode}): {stderr}")
        wav_bytes = Path(tmp_wav).read_bytes()
        log.info(
            "piper: %d bytes (%.2fs audio) for %d chars in %.2fs",
            len(wav_bytes),
            len(wav_bytes) / (SAMPLE_RATE * 2),  # 16-bit = 2 bytes/sample
            len(text),
            time.time() - t0,
        )
        return wav_bytes
    finally:
        try:
            Path(tmp_wav).unlink(missing_ok=True)
        except Exception:
            pass