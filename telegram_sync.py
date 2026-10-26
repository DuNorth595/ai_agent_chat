# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""
Telegram context sync for voice-bridge.

Polls Telegram updates for context the voice assistant needs:
- Recent home-channel messages (so the voice agent knows what was just sent)
- Recent DM messages from the user (so the voice agent knows what's on their mind)

Two surfaces:
  sync_home_context(limit=5)  -> str    formatted block for system prompt
  get_user_dm_context()      -> str    formatted DM block
  sync_home_loop()           -> loop   background poller that updates cache
  notify_telegram(text)      -> str    pushes a notification to home channel
                                    (used for "task complete" pings)

State:
  ~/.config/ai_agent_chat/telegram_sync/state.json  -> {last_update_id, last_dm_id}
  ~/.config/ai_agent_chat/telegram_sync/cache.json  -> cached formatted blocks
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

STATE_DIR = Path(
    os.environ.get("AVA_STATE_DIR")
    or (Path.home() / ".config" / "ai_agent_chat" / "telegram_sync")
)
STATE_FILE = STATE_DIR / "state.json"
CACHE_FILE = STATE_DIR / "cache.json"
HERMES_ENV = Path(
    os.environ.get("AVA_ENV_FILE")
    or (Path(__file__).resolve().parent / ".env")
)


def _load_env() -> dict[str, str]:
    """Load the env file into a dict. Best-effort, never raises."""
    if not HERMES_ENV.exists():
        return {}
    env: dict[str, str] = {}
    try:
        for raw in HERMES_ENV.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return env
    return env


# Token + channel: prefer real env, fall back to .env
_env = _load_env()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or _env.get("TELEGRAM_BOT_TOKEN", "")
HOME_CHANNEL_ID = (
    os.environ.get("TELEGRAM_HOME_CHANNEL")
    or _env.get("TELEGRAM_HOME_CHANNEL", "")
)

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""

POLL_INTERVAL_ACTIVE = 5
POLL_INTERVAL_IDLE = 30
MAX_CACHED_MESSAGES = 10

_state_lock = threading.Lock()
_cache_lock = threading.Lock()

DEFAULT_STATE: dict[str, Any] = {
    "last_update_id": 0,
    "last_dm_update_id": 0,
    "polling_active": False,
}

DEFAULT_CACHE: dict[str, Any] = {
    "home_context": "",
    "dm_context": "",
    "home_updated_at": 0.0,
    "dm_updated_at": 0.0,
}


def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return dict(default)


def _write_json(path: Path, data: dict) -> None:
    _ensure_state_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _telegram_request(method: str, params: dict[str, Any], read_timeout: float = 10.0) -> dict:
    """Raw Telegram Bot API call. Returns parsed JSON or {"ok": False, "error": str}."""
    if not BOT_TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set"}
    url = f"{API_BASE}/{method}"
    encoded = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{encoded}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=read_timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 - pass error to caller
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _format_message(msg: dict) -> str:
    """One-line rendering of a Telegram message for the context prompt."""
    date = msg.get("date", 0)
    ts = datetime.fromtimestamp(date).strftime("%H:%M")
    sender = msg.get("from", {}).get("first_name") or msg.get("author_signature") or "channel"
    text = msg.get("text") or msg.get("caption") or "[non-text]"
    text = text.replace("\n", " ")[:280]
    return f"[{ts}] {sender}: {text}"


def _build_context(messages: list[dict]) -> str:
    if not messages:
        return ""
    lines = [_format_message(m) for m in messages]
    return "\n".join(lines)


def get_updates(offset: int | None = None, limit: int = 50, timeout: int = 0) -> list[dict]:
    params: dict[str, Any] = {"limit": limit, "timeout": timeout, "allowed_updates": '["message"]'}
    if offset is not None:
        params["offset"] = offset
    resp = _telegram_request("getUpdates", params, read_timeout=max(timeout + 5, 10))
    if not resp.get("ok"):
        return []
    return resp.get("result", [])


def _collect_for_chat(updates: list[dict], chat_id: str) -> list[dict]:
    msgs: list[dict] = []
    for upd in updates:
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        chat = msg.get("chat", {})
        if str(chat.get("id")) != str(chat_id):
            continue
        msgs.append(msg)
    return msgs[-MAX_CACHED_MESSAGES:]


def sync_once() -> dict:
    """One polling tick. Returns summary of what changed. Updates state + cache."""
    with _state_lock:
        state = _read_json(STATE_FILE, DEFAULT_STATE)
        last_offset = state["last_update_id"] + 1
    updates = get_updates(offset=last_offset, limit=50)
    if not updates:
        return {"fetched": 0, "home": 0, "dm": 0}

    with _state_lock:
        state = _read_json(STATE_FILE, DEFAULT_STATE)
        max_id = state["last_update_id"]
        for upd in updates:
            if upd["update_id"] > max_id:
                max_id = upd["update_id"]
        state["last_update_id"] = max_id
        _write_json(STATE_FILE, state)

    home_msgs = _collect_for_chat(updates, HOME_CHANNEL_ID)

    dm_msgs: list[dict] = []
    for upd in updates:
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        chat = msg.get("chat", {})
        if chat.get("type") != "private":
            continue
        from_user = msg.get("from", {})
        if from_user.get("is_bot"):
            continue
        dm_msgs.append(msg)
    dm_msgs = dm_msgs[-MAX_CACHED_MESSAGES:]

    with _cache_lock:
        cache = _read_json(CACHE_FILE, DEFAULT_CACHE)
        if home_msgs:
            existing = cache.get("_home_raw", [])
            merged = (existing + home_msgs)[-MAX_CACHED_MESSAGES:]
            cache["_home_raw"] = merged
            cache["home_context"] = _build_context(merged)
            cache["home_updated_at"] = time.time()
        if dm_msgs:
            existing = cache.get("_dm_raw", [])
            merged = (existing + dm_msgs)[-MAX_CACHED_MESSAGES:]
            cache["_dm_raw"] = merged
            cache["dm_context"] = _build_context(merged)
            cache["dm_updated_at"] = time.time()
        _write_json(CACHE_FILE, cache)

    return {"fetched": len(updates), "home": len(home_msgs), "dm": len(dm_msgs)}


def get_home_context(limit_chars: int = 1200) -> str:
    with _cache_lock:
        cache = _read_json(CACHE_FILE, DEFAULT_CACHE)
        ctx = cache.get("home_context", "")
    if not ctx:
        return ""
    return ctx[-limit_chars:]


def get_dm_context(limit_chars: int = 1200) -> str:
    with _cache_lock:
        cache = _read_json(CACHE_FILE, DEFAULT_CACHE)
        ctx = cache.get("dm_context", "")
    if not ctx:
        return ""
    return ctx[-limit_chars:]


def get_full_context(limit_chars: int = 1500) -> str:
    """Combined context block for the system prompt."""
    parts: list[str] = []
    home = get_home_context(limit_chars // 2)
    dm = get_dm_context(limit_chars // 2)
    if home:
        parts.append(f"### Recent in your home Telegram channel:\n{home}")
    if dm:
        parts.append(f"### Recent DMs you sent me:\n{dm}")
    return "\n\n".join(parts)


def notify_telegram(text: str, chat_id: str | None = None) -> dict:
    """Send a notification to a chat (defaults to home channel). Returns API response."""
    target = chat_id or HOME_CHANNEL_ID
    if not target:
        return {"ok": False, "error": "no chat_id and no TELEGRAM_HOME_CHANNEL"}
    params = {"chat_id": target, "text": text}
    resp = _telegram_request("sendMessage", params)
    return resp


# --- background polling loop -----------------------------------------------------

_poller_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _poller_loop() -> None:
    _stop_event.clear()
    while not _stop_event.is_set():
        try:
            sync_once()
        except Exception as e:  # noqa: BLE001
            print(f"[telegram_sync] poll error: {e}", flush=True)
        with _state_lock:
            state = _read_json(STATE_FILE, DEFAULT_STATE)
            interval = POLL_INTERVAL_ACTIVE if state.get("polling_active") else POLL_INTERVAL_IDLE
        _stop_event.wait(interval)


def start_poller() -> bool:
    global _poller_thread
    if _poller_thread and _poller_thread.is_alive():
        return False
    if not BOT_TOKEN or not HOME_CHANNEL_ID:
        print("[telegram_sync] missing TELEGRAM_BOT_TOKEN or TELEGRAM_HOME_CHANNEL", flush=True)
        return False
    _stop_event.clear()
    _poller_thread = threading.Thread(target=_poller_loop, name="telegram-sync-poller", daemon=True)
    _poller_thread.start()
    print("[telegram_sync] poller started", flush=True)
    return True


def stop_poller() -> None:
    _stop_event.set()


def set_polling_active(active: bool) -> None:
    """Mark the polling loop as 'in active session' so it polls faster."""
    with _state_lock:
        state = _read_json(STATE_FILE, DEFAULT_STATE)
        state["polling_active"] = bool(active)
        _write_json(STATE_FILE, state)


def prime_cache() -> dict:
    """Pull recent updates once at startup so the first message has context.

    Uses offset=-100 so we get the last 100 updates (Telegram treats negative
    offsets as 'return the last N unconsumed updates' when timeout=0).
    """
    try:
        with _state_lock:
            state = _read_json(STATE_FILE, DEFAULT_STATE)
            last_update_id = state["last_update_id"]
        # Use -100 only if we haven't consumed anything yet
        offset = -100 if last_update_id == 0 else (last_update_id + 1)
        updates = get_updates(offset=offset, limit=100)
        if not updates:
            return {"fetched": 0, "home": 0, "dm": 0, "note": "no recent updates"}

        with _state_lock:
            state = _read_json(STATE_FILE, DEFAULT_STATE)
            max_id = state["last_update_id"]
            for upd in updates:
                if upd["update_id"] > max_id:
                    max_id = upd["update_id"]
            state["last_update_id"] = max_id
            _write_json(STATE_FILE, state)

        home_msgs = _collect_for_chat(updates, HOME_CHANNEL_ID)
        dm_msgs: list[dict] = []
        for upd in updates:
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            chat = msg.get("chat", {})
            if chat.get("type") != "private":
                continue
            from_user = msg.get("from", {})
            if from_user.get("is_bot"):
                continue
            dm_msgs.append(msg)
        dm_msgs = dm_msgs[-MAX_CACHED_MESSAGES:]

        with _cache_lock:
            cache = _read_json(CACHE_FILE, DEFAULT_CACHE)
            cache["_home_raw"] = home_msgs[-MAX_CACHED_MESSAGES:]
            cache["_dm_raw"] = dm_msgs
            cache["home_context"] = _build_context(cache["_home_raw"])
            cache["dm_context"] = _build_context(cache["_dm_raw"])
            cache["home_updated_at"] = time.time()
            cache["dm_updated_at"] = time.time()
            _write_json(CACHE_FILE, cache)

        return {"fetched": len(updates), "home": len(home_msgs), "dm": len(dm_msgs)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


if __name__ == "__main__":
    # Manual CLI for testing: python -m telegram_sync prime
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "prime"
    if cmd == "prime":
        print(json.dumps(prime_cache(), indent=2))
    elif cmd == "home":
        print(get_home_context())
    elif cmd == "dm":
        print(get_dm_context())
    elif cmd == "loop":
        start_poller()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop_poller()
    else:
        print(f"unknown cmd: {cmd}", file=sys.stderr)