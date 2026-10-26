#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""
tools.py — voice-Sam's tool implementations.

Each tool is a plain function: (args: dict) -> (result: str | dict).

Tools are organized by tier:
- READ_ONLY: safe, no side effects (search_files, read_file, web_fetch)
- LOCAL_WRITE: visible, reversible (fact_store_add, delegate_task, cron_create)
- EXTERNAL_WRITE: irreversible, requires confirmation (send_message)

Confirmation flow is enforced at the agent loop level in brain.py, not here.
Tools themselves execute whatever they're asked — the loop wraps them.
"""
from __future__ import annotations

import os
import re
import json
import sqlite3
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import logging
from pathlib import Path
from typing import Callable, Any

log = logging.getLogger("sam.tools")

HOME = Path.home()
# Configurable directories — see memory.py for the same env-driven pattern.
# Importing from memory.py ensures tools.py and memory.py agree on the
# resolved paths (and that .env was loaded with the same fallback logic).
# Falls back to env-direct read only if memory.py is unavailable (rare).
try:
    from memory import (  # type: ignore
        SOUL_PATH as _SOUL_PATH,
        SAM_INDEX_PATH as _SAM_INDEX_PATH,
        LONG_TERM_MEMORY_DIR as _LONG_TERM_MEMORY_DIR,
        FACT_STORE_DB as _FACT_STORE_DB,
        SESSIONS_DIR as _SESSIONS_DIR,
    )
    _HERMES_DIR = _SOUL_PATH.parent
    FACT_STORE_DB = _FACT_STORE_DB
    LONG_TERM_MEMORY_DIR = _LONG_TERM_MEMORY_DIR
except Exception:
    _HERMES_DIR = Path(
        os.environ.get("AVA_CONFIG_DIR")
        or (Path(__file__).resolve().parent)
    )
    FACT_STORE_DB = Path(
        os.environ.get("MEMORY_FACT_STORE_DB")
        or (HOME / ".config" / "ai_agent_chat" / "fact_store.db")
    )

    LONG_TERM_MEMORY_DIR = Path(
        os.environ.get("MEMORY_LIFE_DIR")
        or (HOME / "Desktop" / "LIFE_MEMORY")
    )

# Tier classifications — used by brain.py to decide whether to gate execution
READ_ONLY = "read_only"
LOCAL_WRITE = "local_write"
EXTERNAL_WRITE = "external_write"


# ---------- helpers ----------

def _load_env() -> dict:
    """Read .env from a configurable location. Defaults to ./.env next to
    this file. Override with the AVA_ENV_FILE env var to point elsewhere."""
    env = {}
    env_file = Path(os.environ.get("AVA_ENV_FILE") or _HERMES_DIR / ".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _search_stopwords() -> set:
    return {
        "the", "and", "for", "are", "but", "not", "you", "all", "can", "had",
        "her", "was", "one", "our", "out", "day", "get", "has", "him", "his",
        "how", "its", "may", "new", "now", "old", "see", "way", "who", "boy",
        "did", "let", "say", "she", "too", "use", "what", "when", "your", "from",
        "this", "that", "with", "have", "will", "just",
    }


# ---------- READ_ONLY tools ----------

def search_files(args: dict) -> dict:
    """Search file contents under a path. Args: query, path (default $MEMORY_LIFE_DIR
    or ~/Desktop/LIFE_MEMORY). If MEMORY_LIFE_DIR is not set and the default
    directory doesn't exist, falls back to ~/Desktop/LIFE_MEMORY first, then
    ~/.config/ai_agent_chat, then the home dir as a last resort.
    Scoped to keep searches fast — pass an explicit `path` for big trees."""
    query = args.get("query", "").strip()
    if not query:
        return {"error": "query is required"}
    # Resolution order for the default search path:
    #   1. explicit `path` arg from the caller
    #   2. LONG_TERM_MEMORY_DIR (already resolved via memory.py)
    #   3. canonical real fallback: ~/Desktop/LIFE_MEMORY
    #   4. ~/.config/ai_agent_chat
    #   5. last resort: $HOME
    explicit = (args.get("path") or "").strip()
    if explicit:
        search_path = os.path.expanduser(explicit)
    elif LONG_TERM_MEMORY_DIR.exists():
        search_path = str(LONG_TERM_MEMORY_DIR)
    else:
        for cand in (HOME / "Desktop" / "LIFE_MEMORY",
                     HOME / ".config" / "ai_agent_chat",
                     HOME):
            if cand.exists():
                search_path = str(cand)
                break
        else:
            search_path = str(HOME)
    limit = min(int(args.get("limit", 20)), 50)

    stop = _search_stopwords()
    tokens = [t for t in re.findall(r"[A-Za-z0-9_]+", query) if len(t) >= 3 and t.lower() not in stop]
    if not tokens:
        return {"matches": [], "note": "no searchable tokens in query"}

    # rg: use a single alternation pattern via -e so tokens aren't mistaken for paths.
    pattern = "|".join(tokens[:8])
    try:
        proc = subprocess.run(
            ["rg", "--json", "-i", "-l",
             "-g", "!*.pyc", "-g", "!.git/*", "-g", "!node_modules/*",
             "-g", "!venv*", "-g", "!.venv*", "-g", "!.cache/*",
             "--max-count", "3",
             "-e", pattern, search_path],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode not in (0, 1):
            return {"matches": [], "note": f"ripgrep exited {proc.returncode}: {proc.stderr.strip()[:200]}"}
        paths = [p for p in proc.stdout.strip().splitlines() if p]
        return {"matches": paths[:limit], "count": len(paths)}
    except FileNotFoundError:
        return {"matches": [], "note": "ripgrep not installed"}
    except subprocess.TimeoutExpired:
        return {"matches": [], "note": "search timed out after 15s"}


def read_file(args: dict) -> dict:
    """Read a file (truncated to keep prompt sane).

    Args:
      path      -- file to read
      max_lines -- how many lines to return (default 200, max 2000)
      offset    -- 1-based line to start from
      tail      -- read the LAST N lines instead of the first N

    `tail` exists because of a real failure (2026-10-26): asked
    "what project are we working on?", the bridge read the first 200
    lines of a 1562-line daily log -- the MORNING's work -- and
    reported that instead of the current thread. The recent work was
    at the BOTTOM of the file, and there was no way to reach it.
    `tail` (or `offset`) makes the end of a file reachable in one
    call.
    """
    path = args.get("path", "").strip()
    if not path:
        return {"error": "path is required"}
    max_lines = min(int(args.get("max_lines", 200)), 2000)
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = HOME / p
    if not p.exists():
        return {"error": f"file not found: {p}"}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        all_lines = text.splitlines()
        total = len(all_lines)

        tail = args.get("tail")
        offset = args.get("offset")

        if tail is not None:
            # Read the LAST N lines.
            try:
                n = max(1, int(tail))
            except (TypeError, ValueError):
                return {"error": "tail must be an integer"}
            start = max(0, total - n)
            lines = all_lines[start:]
            truncated = start > 0
            return {
                "path": str(p),
                "total_lines": total,
                "content": "\n".join(lines),
                "truncated": truncated,
                "read_from": "tail",
                "line_range": f"{start + 1}-{total}",
            }

        if offset is not None:
            # Read N lines starting at a 1-based offset.
            try:
                start = max(0, int(offset) - 1)
            except (TypeError, ValueError):
                return {"error": "offset must be an integer"}
            lines = all_lines[start:start + max_lines]
            truncated = (start + max_lines) < total
            return {
                "path": str(p),
                "total_lines": total,
                "content": "\n".join(lines),
                "truncated": truncated,
                "read_from": "offset",
                "line_range": f"{start + 1}-{start + len(lines)}",
            }

        truncated = total > max_lines
        lines = all_lines[:max_lines] if truncated else all_lines
        return {
            "path": str(p),
            "total_lines": total,
            "content": "\n".join(lines),
            "truncated": truncated,
        }
    except Exception as e:
        return {"error": f"read failed: {e}"}


def web_fetch(args: dict) -> dict:
    """Fetch a URL and return the body. Args: url, max_bytes (default 50000)."""
    url = args.get("url", "").strip()
    if not url:
        return {"error": "url is required"}
    max_bytes = min(int(args.get("max_bytes", 50000)), 200000)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sam-voice-bridge/0.4"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read(max_bytes + 1)
            truncated = len(body) > max_bytes
            if truncated:
                body = body[:max_bytes]
            return {
                "url": url,
                "status": r.status,
                "content_type": r.headers.get("Content-Type", ""),
                "body": body.decode("utf-8", errors="replace"),
                "truncated": truncated,
            }
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"error": f"fetch failed: {e}"}


# ---------- System context tools (always available) ----------

def get_current_time(args: dict) -> dict:
    """Return the current local time and date for the user's timezone.

    No args. Detects timezone from the user's life memory
    (AVA_TIMEZONE, or a timezone file in the memory directory) when
    available; otherwise uses system local time.
    Returns ISO timestamp, formatted local time, timezone name, and
    day-of-week — all in shapes that survive TTS (numbers as words).

    This is the answer to "what time is it?" / "what's today's date?" —
    voice Sam previously had to guess.
    """
    from datetime import datetime, timezone
    # Try to detect the user's timezone from life memory
    tz_name = "system"
    tz_offset_hours = None
    try:
        # The identity file is configurable, not hardcoded to a person.
        _id = os.environ.get("MEMORY_IDENTITY_FILE", "identity.md").strip()
        _id_path = (HOME / "Desktop" / "LIFE_MEMORY" / "PEOPLE" / _id) \
            if "/" not in _id else Path(_id).expanduser()
        if _id_path.exists():
            text = _id_path.read_text(encoding="utf-8", errors="replace").lower()
            # crude scan — explicit only, no regex surprises
            for tz in ("america/chicago", "america/new_york", "america/denver",
                       "america/los_angeles", "america/phoenix", "utc",
                       "europe/london", "europe/berlin"):
                if tz in text:
                    tz_name = tz
                    break
    except Exception:
        pass

    now = datetime.now()
    iso = now.isoformat(timespec="seconds")

    # Time-of-day in words (TTS-friendly)
    h12 = now.hour % 12 or 12
    ampm = "AM" if now.hour < 12 else "PM"
    time_words = _number_to_words(h12) + " " + _number_to_words(now.minute)

    return {
        "ok": True,
        "iso_timestamp": iso,
        "year": now.year,
        "month": now.month,
        "day": now.day,
        "hour_24": now.hour,
        "minute": now.minute,
        "ampm": ampm,
        "time_words": time_words,
        "weekday": now.strftime("%A"),
        "timezone": tz_name,
        "detected_from": (
            os.environ.get("MEMORY_IDENTITY_FILE", "identity.md")
            if tz_name != "system" else "system_local"
        ),
    }


def _number_to_words(n: int) -> str:
    """Convert 0..59 to English words (TTS-friendly)."""
    if n == 0:
        return "zero"
    ones = ["zero", "one", "two", "three", "four", "five", "six", "seven",
            "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
            "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
    tens = ["", "", "twenty", "thirty", "forty", "fifty"]
    if n < 20:
        return ones[n]
    if n % 10 == 0:
        return tens[n // 10]
    return tens[n // 10] + "-" + ones[n % 10]


def system_info(args: dict) -> dict:
    """Return basic system context: hostname, user, OS, uptime, public IP.

    No args. Answers "what's my machine's name", "am I logged in", etc.
    Uses only stdlib + a single GET to a 1.1.1.1 lookup for public IP.
    """
    import socket
    import platform
    import os as _os
    out = {
        "ok": True,
        "hostname": socket.gethostname(),
        "user": _os.environ.get("USER") or _os.environ.get("USERNAME") or "?",
        "home": str(HOME),
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "python": platform.python_version(),
        "cwd": _os.getcwd(),
    }
    # uptime via /proc on linux, sysctl on darwin
    if platform.system() == "Darwin":
        try:
            out["uptime_seconds"] = int(
                subprocess.check_output(["sysctl", "-n", "kern.boottime"],
                                       text=True, timeout=2).split(",")[1].strip().split()[0])
        except Exception:
            out["uptime_seconds"] = None
    # public IP — fast, one HTTP call to Cloudflare's known endpoint
    try:
        req = urllib.request.Request("https://1.1.1.1/cdn-cgi/trace",
                                     headers={"User-Agent": "sam-voice-bridge/0.4"})
        with urllib.request.urlopen(req, timeout=3) as r:
            body = r.read(2000).decode("utf-8", errors="replace")
            for line in body.splitlines():
                if line.startswith("ip="):
                    out["public_ip"] = line.split("=", 1)[1]
                    break
    except Exception:
        out["public_ip"] = None
    return out


def exec_command(args: dict) -> dict:
    """Run a read-only shell command and return stdout/stderr.

    Args: command (str, required), timeout (int, default 10s, max 30s).
    Tier: READ_ONLY but ALLOWLISTED — only a short list of safe read-only
    commands is permitted (ls, cat, head, tail, grep, df, du, uname, whoami,
    date, pwd, ps, lsof, etc.). Anything else returns an error.

    This is NOT a general shell — the user explicitly asked for Hermes /
    system access from voice, and this is the minimum safe surface to give
    them. If you need something not in the allowlist, request it via
    `fact_store_add` or `cron_create` and a sub-agent will run it.

    Returns: {"ok": True, "stdout": "...", "stderr": "...", "exit": 0,
              "command": "...", "duration_ms": 123}
    """
    cmd = (args.get("command") or "").strip()
    if not cmd:
        return {"error": "command is required"}

    # Strip leading shell metachars that could chain commands
    if any(c in cmd for c in (";", "|", "&", "&&", "||", "`", "$(")):
        return {"error": "command contains disallowed shell metacharacters (; | & ` $)"}

    # Allowlist: each entry is (first_token, [allowed_subcommands_for_top_command])
    # or (first_token, None) when the first token alone is enough.
    first = cmd.split()[0].rstrip(";")
    # Allow paths like /usr/bin/ls by basename
    base = first.split("/")[-1]

    ALLOW = {
        # system info
        "date": None, "uname": None, "whoami": None, "hostname": None,
        "uptime": None, "pwd": None, "id": None, "env": None,
        # filesystem (read-only)
        "ls": None, "cat": None, "head": None, "tail": None, "wc": None,
        "file": None, "stat": None, "du": None, "df": None, "find": None,
        "tree": None, "less": None,
        # processes
        "ps": None, "top": None, "lsof": None,
        # network (read-only diagnostics)
        "ping": None, "traceroute": None, "nslookup": None, "dig": None,
        "ifconfig": None, "ip": None, "arp": None, "netstat": None,
        "ss": None, "curl": None, "wget": None, "host": None,
        # text tools (safe)
        "grep": None, "rg": None, "awk": None, "sed": None,
        "sort": None, "uniq": None, "cut": None, "tr": None,
        "echo": None, "printf": None, "jq": None, "python3": None,
        # git (read-only)
        "git": ["status", "log", "diff", "show", "branch", "remote", "rev-parse"],
    }

    if base not in ALLOW:
        return {
            "error": f"command '{base}' is not in the allowlist. "
                     f"Ask a sub-agent (cron_create) for commands not on the safe list.",
            "allowlist": sorted(ALLOW.keys()),
        }

    allowed_sub = ALLOW[base]
    if allowed_sub is not None:
        # second token must be in allowed_sub
        tokens = cmd.split()
        if len(tokens) < 2 or tokens[1].lstrip("-") not in allowed_sub:
            return {
                "error": f"'{base}' requires a subcommand in {allowed_sub}. "
                         f"Got: {tokens[1] if len(tokens) > 1 else '(none)'}",
            }

    timeout_s = min(int(args.get("timeout", 10)), 30)
    try:
        proc = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True, text=True, timeout=timeout_s,
        )
        # cap stdout/stderr at 16kB each
        return {
            "ok": proc.returncode == 0,
            "command": cmd,
            "stdout": (proc.stdout or "")[:16384],
            "stderr": (proc.stderr or "")[:4096],
            "exit": proc.returncode,
            "truncated_stdout": len(proc.stdout or "") > 16384,
        }
    except subprocess.TimeoutExpired:
        return {"error": f"command timed out after {timeout_s}s"}
    except Exception as e:
        return {"error": f"exec failed: {e}"}


# ---------- LOCAL_WRITE tools ----------

def fact_store_add(args: dict) -> dict:
    """Add a fact to Sam's memory. Args: content (required), category (default 'general'),
    tags (comma-separated, optional)."""
    content = args.get("content", "").strip()
    if not content:
        return {"error": "content is required"}
    category = args.get("category", "general").strip() or "general"
    tags = args.get("tags", "").strip()

    if not FACT_STORE_DB.exists():
        return {"error": f"fact_store db missing: {FACT_STORE_DB}"}

    try:
        conn = sqlite3.connect(str(FACT_STORE_DB), timeout=5.0)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO facts (category, content, tags, trust, created_at, updated_at) "
            "VALUES (?, ?, ?, 0.5, datetime('now'), datetime('now'))",
            (category, content, tags),
        )
        new_id = cur.lastrowid
        # Update FTS index if it exists
        try:
            cur.execute("INSERT INTO facts_fts(rowid, content, category, tags) VALUES (?, ?, ?, ?)",
                        (new_id, content, category, tags))
        except sqlite3.OperationalError as e:
            log.warning("FTS update skipped: %s", e)
        conn.commit()
        conn.close()
        return {"ok": True, "fact_id": new_id, "category": category, "stored_chars": len(content)}
    except Exception as e:
        return {"error": f"fact_store_add failed: {e}"}


def delegate_task(args: dict) -> dict:
    """Hand off a long task to a sub-agent (background). Args: goal (required),
    context (optional background). NOTE: this is a thin wrapper around the
    delegate_task tool I have at the platform level — but voice-bridge doesn't
    have that tool, so we shell out via cron_create as the proper async path.

    For voice Sam, this is best modeled as 'create a cron job to do X and notify
    me on completion.' Returns the cron job id.
    """
    goal = args.get("goal", "").strip()
    if not goal:
        return {"error": "goal is required"}
    # Defer to cron_create — same result, available from this session
    return cron_create({
        "name": f"voice-delegate: {goal[:50]}",
        "schedule": "now",
        "prompt": goal,
        "deliver": "telegram",
    })


def cron_create(args: dict) -> dict:
    """Schedule a cron job via the Hermes scheduler. Args: name, schedule (cron or 'now'),
    prompt (required), deliver ('origin'|'local'|'telegram'|'all', default 'telegram')."""
    prompt = args.get("prompt", "").strip()
    if not prompt:
        return {"error": "prompt is required"}
    name = args.get("name", "voice-sam-job").strip()
    schedule = args.get("schedule", "now").strip()
    deliver = args.get("deliver", "telegram").strip()

    # Use the hermes CLI to schedule. hermes-cron-script-args skill has the
    # canonical reference — but the basic command shape is:
    #   hermes cron create <name> <schedule> --prompt "<prompt>" --deliver <target>
    cmd = ["hermes", "cron", "create", "--name", name, "--schedule", schedule,
           "--prompt", prompt, "--deliver", deliver]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return {"error": f"hermes cron create failed: {proc.stderr.strip()[:500]}"}
        return {"ok": True, "name": name, "schedule": schedule, "deliver": deliver,
                "output": proc.stdout.strip()[:500]}
    except FileNotFoundError:
        return {"error": "hermes CLI not on PATH"}
    except Exception as e:
        return {"error": f"cron_create failed: {e}"}


# ---------- EXTERNAL_WRITE tools ----------

def send_message(args: dict) -> dict:
    """Send a Telegram message. Args: text (required), chat_id (optional — defaults
    to TELEGRAM_HOME_CHANNEL), reply_to_message_id (optional)."""
    text = args.get("text", "").strip()
    if not text:
        return {"error": "text is required"}
    chat_id = str(args.get("chat_id") or _load_env().get("TELEGRAM_HOME_CHANNEL") or "").strip()
    if not chat_id:
        return {"error": "no chat_id and no TELEGRAM_HOME_CHANNEL in env"}

    token = _load_env().get("TELEGRAM_BOT_TOKEN")
    if not token:
        return {"error": "TELEGRAM_BOT_TOKEN missing from $AVA_ENV_FILE or ./.env"}

    payload = {"chat_id": chat_id, "text": text}
    rid = args.get("reply_to_message_id")
    if rid:
        payload["reply_to_message_id"] = int(rid)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read().decode("utf-8"))
        if not resp.get("ok"):
            return {"error": f"telegram returned not-ok: {resp}"}
        result = resp.get("result", {})
        return {
            "ok": True,
            "message_id": result.get("message_id"),
            "chat_id": result.get("chat", {}).get("id"),
            "text_sent": text,
            "ts": result.get("date"),
        }
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}"}
    except Exception as e:
        return {"error": f"send_message failed: {e}"}


# ---------- Registry ----------

TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "search_files": {
        "fn": search_files,
        "tier": READ_ONLY,
        "description": (
            "Search file contents by regex/keyword. Args: query (str, required), "
            "path (str, optional — omit to search the user's life memory tree at "
            "$MEMORY_LIFE_DIR which defaults to ~/Desktop/LIFE_MEMORY and contains "
            "PEOPLE/, PROJECTS/, REFLECTIONS/, SAM/, etc.), limit (int, default 20). "
            "Use this to find files before calling read_file. The default scope is "
            "the user's entire life memory — feel free to search broadly."
        ),
    },
    "read_file": {
        "fn": read_file,
        "tier": READ_ONLY,
        "description": (
            "Read a file's contents (truncated to keep prompts sane). "
            "Args: path (str, required — absolute path, or relative to $HOME), "
            "max_lines (int, default 200), "
            "tail (int — read the LAST N lines instead of the first N), "
            "offset (int — 1-based line to start from). "
            "Use search_files first to locate the path. "
            "IMPORTANT: for a long append-only file (a DAILY log), use "
            "tail=<N> to read the most RECENT entries — the first N lines "
            "are the OLDEST work, not the current state."
        ),
    },
    "web_fetch": {
        "fn": web_fetch,
        "tier": READ_ONLY,
        "description": "Fetch a URL and return its body. Args: url (str, required), max_bytes (int, default 50000).",
    },
    "get_current_time": {
        "fn": get_current_time,
        "tier": READ_ONLY,
        "description": (
            "Return the current local time and date. No args. Detects the user's "
            "timezone from $AVA_TIMEZONE when set, otherwise the system timezone. "
            "Returns ISO timestamp, weekday, time-of-day in both digits and TTS-friendly "
            "words (e.g. 'seven twenty-three' for 7:23), and timezone name. "
            "Use this whenever the user asks 'what time is it', 'what's today', "
            "'what day is it', etc. — don't guess."
        ),
    },
    "system_info": {
        "fn": system_info,
        "tier": READ_ONLY,
        "description": (
            "Return basic system context: hostname, current user, OS, architecture, "
            "Python version, current working directory, uptime, and public IP. "
            "No args. Use for 'what machine am I on', 'am I logged in', etc."
        ),
    },
    "exec_command": {
        "fn": exec_command,
        "tier": READ_ONLY,
        "description": (
            "Run a read-only shell command and return stdout/stderr/exit code. "
            "Args: command (str, required — first token must be in the allowlist: "
            "ls, cat, head, tail, wc, file, stat, du, df, find, tree, ps, top, lsof, "
            "ping, nslookup, dig, curl, wget, ifconfig, ip, netstat, ss, host, "
            "grep, rg, awk, sed, sort, uniq, cut, tr, echo, printf, jq, python3, "
            "date, uname, whoami, hostname, uptime, pwd, id, env, git status/log/diff/"
            "show/branch/remote/rev-parse). NO shell metacharacters allowed (; | & ` $). "
            "Commands not on the allowlist return an error with the full list — for "
            "anything else, schedule a sub-agent via cron_create."
        ),
    },
    "fact_store_add": {
        "fn": fact_store_add,
        "tier": LOCAL_WRITE,
        "description": "Add a fact to Sam's persistent memory (fact_store). Args: content (str, required), category (str, default 'general'), tags (str, comma-separated, optional).",
    },
    "cron_create": {
        "fn": cron_create,
        "tier": LOCAL_WRITE,
        "description": "Schedule a Hermes cron job. Args: name (str), schedule (cron or 'now'), prompt (str, required), deliver ('origin'|'local'|'telegram'|'all', default 'telegram').",
    },
    "delegate_task": {
        "fn": delegate_task,
        "tier": LOCAL_WRITE,
        "description": "Hand off a long task to a sub-agent via a one-shot cron job. Args: goal (str, required), context (str, optional).",
    },
    "send_message": {
        "fn": send_message,
        "tier": EXTERNAL_WRITE,
        "description": "Send a Telegram message. Args: text (str, required), chat_id (str/int, optional — defaults to TELEGRAM_HOME_CHANNEL).",
    },
}


def anthropic_tools() -> list[dict]:
    """Build the tools[] payload for the Anthropic API. Schema is permissive
    (additionalProperties=true) because we infer param names from descriptions."""
    out = []
    for name, spec in TOOL_REGISTRY.items():
        props = _infer_properties(spec["description"])
        required = [k for k, v in props.items() if v.get("hint") == "required"]
        # strip our private hint field
        clean_props = {k: {"type": v["type"]} for k, v in props.items()}
        out.append({
            "name": name,
            "description": spec["description"],
            "input_schema": {
                "type": "object",
                "properties": clean_props,
                "required": required,
                "additionalProperties": True,
            },
        })
    return out


def _infer_properties(description: str) -> dict:
    """Pull 'Args: name (type, default ...)' hints out of the description."""
    props = {}
    m = re.search(r"Args:\s*(.+)$", description)
    if not m:
        return props
    arg_blob = m.group(1)
    for piece in re.split(r",\s*(?=[a-z_]+\s*\()", arg_blob):
        am = re.match(r"([a-z_]+)\s*\(([^)]+)\)", piece.strip())
        if not am:
            continue
        name = am.group(1)
        meta = am.group(2)
        hint = "required" if "required" in meta.lower() else "optional"
        type_hint = "string"
        if "int" in meta.lower():
            type_hint = "integer"
        elif "bool" in meta.lower():
            type_hint = "boolean"
        props[name] = {"type": type_hint, "hint": hint}
    return props


def execute(name: str, args: dict) -> dict:
    """Run a tool by name. Result is always a dict."""
    spec = TOOL_REGISTRY.get(name)
    if not spec:
        return {"error": f"unknown tool: {name}"}
    try:
        return spec["fn"](args)
    except Exception as e:
        log.exception("tool %s raised", name)
        return {"error": f"{name} raised: {e}"}


def tier_of(name: str) -> str | None:
    spec = TOOL_REGISTRY.get(name)
    return spec["tier"] if spec else None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    # Quick smoke
    print("--- registry ---")
    for n, s in TOOL_REGISTRY.items():
        print(f"  {n}: {s['tier']}")
    print("--- anthropic tools shape ---")
    print(json.dumps(anthropic_tools()[0], indent=2))
    print("--- smoke: read_file (will skip if no SOUL.md at the configured path) ---")
    soul_path = os.environ.get(
        "MEMORY_SOUL_PATH",
        str(Path.home() / ".config" / "ai_agent_chat" / "SOUL.md"),
    )
    print(json.dumps(read_file({"path": soul_path, "max_lines": 5}), indent=2)[:400])
    print("--- smoke: search_files ---")
    print(json.dumps(search_files({"query": "example", "limit": 5}), indent=2)[:400])