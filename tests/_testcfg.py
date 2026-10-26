# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""
Shared test configuration.

Resolves the Piper binary and voice model at runtime so the test suite
runs on any machine. Override with environment variables if your
layout differs:

    AVA_TEST_PIPER         path to the `piper` executable
    AVA_TEST_PIPER_MODEL   path to a *.onnx voice model

If either is not set, a short list of common locations is searched.
The tests that synthesize speech are skipped (not failed) when no
Piper is available, so the suite still runs on a machine that only
has the server side installed.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# --- Piper executable -------------------------------------------------
_piper_env = os.environ.get("AVA_TEST_PIPER", "").strip()
PIPER = _piper_env or ""


def _find_piper() -> str:
    if PIPER:
        return PIPER
    # A venv inside the repo, then PATH, then the usual brew prefix.
    for cand in (
        REPO / ".venv" / "bin" / "piper",
        REPO / ".venv-iris" / "bin" / "piper",
    ):
        if cand.is_file():
            return str(cand)
    which = shutil.which("piper")
    if which:
        return which
    return ""


PIPER = _find_piper()

# --- Voice model ------------------------------------------------------
_model_env = os.environ.get("AVA_TEST_PIPER_MODEL", "").strip()
MODEL = _model_env or ""


def _read_bridge_env() -> dict:
    """Read the bridge's own .env (if present) without requiring dotenv."""
    env: dict = {}
    env_file = Path(os.environ.get("AVA_ENV_FILE") or (REPO / ".env"))
    if not env_file.is_file():
        return env
    try:
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip().strip("'\"")
    except Exception:
        pass
    return env


def _configured_model_dir() -> Path:
    """PIPER_MODEL_DIR from the environment, then from the bridge's .env."""
    raw = os.environ.get("PIPER_MODEL_DIR") or _read_bridge_env().get("PIPER_MODEL_DIR")
    if not raw:
        return Path()
    return Path(os.path.expanduser(raw.strip()))


def _find_model() -> str:
    if MODEL:
        return MODEL
    homes = [
        _configured_model_dir(),
        Path.home() / ".local" / "share" / "piper" / "voices",
        Path.home() / ".config" / "ai_agent_chat" / "piper",
        Path.home() / ".hermes" / "piper",
        Path("/usr/local/share/piper/voices"),
        Path("/opt/homebrew/share/piper/voices"),
    ]
    preferred = ("amy", "libritts_r", "jenny", "hfc_female")
    found = []
    for d in homes:
        if not d or not d.is_dir():
            continue
        for f in sorted(d.glob("*.onnx")):
            # A 15-byte .onnx is a failed download (LFS pointer / stub).
            if f.stat().st_size > 1024:
                found.append(str(f))
    for pref in preferred:
        for f in found:
            if pref in Path(f).name:
                return f
    return found[0] if found else ""


MODEL = _find_model()

# --- Capability check -------------------------------------------------
HAVE_PIPER = bool(PIPER) and bool(MODEL) and Path(MODEL).is_file()


def synth(phrase: str, path: str):
    """Piper -> 16kHz mono PCM16 bytes. Returns None if Piper is absent."""
    if not HAVE_PIPER:
        return None
    if not Path(path).exists():
        try:
            subprocess.run(
                [PIPER, "--model", MODEL, "--output_file", path],
                input=phrase.encode(), check=True, capture_output=True,
            )
        except subprocess.CalledProcessError:
            return None
    import struct
    import wave
    with wave.open(path, "rb") as w:
        pcm = w.readframes(w.getnframes())
        rate = w.getframerate()
    n_in = len(pcm) // 2
    samples = struct.unpack("<%dh" % n_in, pcm)
    n_out = int(n_in * 16000 / rate)
    out = bytearray()
    for i in range(n_out):
        src = i * rate / 16000
        i0 = int(src)
        i1 = min(i0 + 1, n_in - 1)
        frac = src - i0
        v = int(samples[i0] * (1 - frac) + samples[i1] * frac)
        out += struct.pack("<h", max(-32768, min(32767, v)))
    return bytes(out)


def require_piper() -> None:
    """Exit cleanly (not an error) when Piper is unavailable."""
    if not HAVE_PIPER:
        print("  SKIP: Piper not found. Set AVA_TEST_PIPER and")
        print("        AVA_TEST_PIPER_MODEL to run the speech tests.")
        sys.exit(0)
