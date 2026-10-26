#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""
memory.py — placeholder memory layer for the voice bridge.

This module is intentionally **minimal and generic**. It defines the
public symbols (`build_memory_context`, `SOUL_PATH`, `SAM_INDEX_PATH`,
`FACT_STORE_DB`, `LONG_TERM_MEMORY_DIR`) that the bridge expects to import, but
it does not hardcode any personal identity, family names, or project
folders. Each path is configurable via environment variables.

For a useful voice agent, fork this module and inject your own memory
sources. Two common patterns:

1. **Personality layer.** Load a markdown file describing the agent's
   voice, opinions, and identity. Read it from `MEMORY_SOUL_PATH` and
   include it in the system prompt. See the placeholder below for the
   minimal version.

2. **Long-term context layer.** Load an index of facts about the user
   (people, projects, recurring topics) from `MEMORY_INDEX_PATH`. When
   a user query hits a relevant keyword, pull the matching file under
   `MEMORY_LIFE_DIR` and inject it.

Both layers should be capped (see `MAX_*_CHARS` below) so they don't
blow the model context window.

Env vars (all optional — defaults are sensible):
    MEMORY_SOUL_PATH        personality/voice rules markdown
                            (default: ~/.config/ai_agent_chat/SOUL.md)
    MEMORY_INDEX_PATH       index of memory files
                            (default: ~/.config/ai_agent_chat/MEMORY_INDEX.md)
    MEMORY_LIFE_DIR         directory of long-term memory files
                            (default: ~/.local/share/ai_agent_chat/life)
    MEMORY_FACT_STORE_DB    SQLite db of operational facts
                            (default: ~/.local/share/ai_agent_chat/facts.db)
    MEMORY_SESSIONS_DIR     session history directory
                            (default: ~/.local/share/ai_agent_chat/sessions)

The defaults sit under XDG-style paths and contain nothing by default.
Override them in `.env` if you want to wire in your own files.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    # Load the bridge's own .env. Nothing else is read implicitly —
    # every memory path is configurable via the MEMORY_* variables.
    _here = Path(__file__).resolve().parent
    load_dotenv(_here / ".env", override=False)
except Exception:  # python-dotenv missing → fall back to real env only
    def load_dotenv(*_a, **_kw):  # type: ignore
        return False

log = logging.getLogger("ava.memory")


# ---- Paths (all env-driven, XDG defaults) ----

def _env_path(name: str, default: Path, fallback: Optional[Path] = None) -> Path:
    val = os.environ.get(name)
    if val:
        return Path(val).expanduser()
    if default.exists():
        return default
    if fallback is not None and fallback.exists():
        return fallback
    return default


def _first_existing(*paths: Path) -> Optional[Path]:
    """Return the first existing path, or None."""
    for p in paths:
        if p is not None and p.exists():
            return p
    return None


def _xdg_config() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "ai_agent_chat"


def _xdg_data() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "ai_agent_chat"


SOUL_PATH = _env_path("MEMORY_SOUL_PATH",
    _xdg_config() / "SOUL.md",
    _first_existing(Path.home() / ".config" / "ai_agent_chat" / "SOUL.md"))
SAM_INDEX_PATH = _env_path("MEMORY_INDEX_PATH",
    _xdg_config() / "SAM_INDEX.md",
    _first_existing(Path.home() / ".config" / "ai_agent_chat" / "MEMORY.md"))
LONG_TERM_MEMORY_DIR = _env_path("MEMORY_LIFE_DIR",
    _xdg_data() / "life",
    _first_existing(Path.home() / ".config" / "ai_agent_chat" / "memories"))
FACT_STORE_DB = _env_path("MEMORY_FACT_STORE_DB",
    _xdg_data() / "facts.db",
    _first_existing(Path.home() / ".config" / "ai_agent_chat" / "fact_store.db"))
SESSIONS_DIR = _env_path("MEMORY_SESSIONS_DIR",
    _xdg_data() / "sessions",
    _first_existing(Path.home() / ".config" / "ai_agent_chat" / "sessions"))

# Cap each layer to keep the system prompt sane.
MAX_SOUL_CHARS = 3500
MAX_INDEX_CHARS = 2500
MAX_FACTS_CHARS = 2400
MAX_LIFE_CHARS = 1800


# ---- Read helpers ----

def _safe_read(path: Path, max_chars: int = 0) -> str:
    """Read a file, optionally truncating."""
    try:
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        if max_chars and len(text) > max_chars:
            text = text[:max_chars] + "\n[…truncated…]"
        return text.strip()
    except Exception as e:
        log.warning("memory: failed to read %s: %s", path, e)
        return ""


def _read_fact_store_for_query(query: str, max_chars: int = MAX_FACTS_CHARS) -> str:
    """FTS5 search the fact_store for facts relevant to the query.

    Returns empty string if the db doesn't exist or the query has no
    usable tokens. The schema (`facts` + `facts_fts`) matches what
    `fact_store_add` writes — see tools.py.
    """
    if not FACT_STORE_DB.exists():
        return ""
    stop = {
        "the", "and", "for", "are", "but", "not", "you", "all", "can", "had",
        "her", "was", "one", "our", "out", "day", "get", "has", "him", "his",
        "how", "its", "may", "new", "now", "old", "see", "way", "who", "boy",
        "did", "let", "say", "she", "too", "use", "what", "when", "your",
        "from", "this", "that", "with", "have", "will", "just",
    }
    tokens = [
        t for t in re.findall(r"[A-Za-z0-9_]+", query)
        if len(t) >= 3 and t.lower() not in stop
    ]
    if not tokens:
        return ""
    quoted = " OR ".join(f'"{t}"' for t in tokens[:8])
    try:
        conn = sqlite3.connect(str(FACT_STORE_DB), timeout=2.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT f.fact_id, f.category, substr(f.content, 1, 400) AS snippet "
            "FROM facts_fts ft JOIN facts f ON f.fact_id = ft.rowid "
            "WHERE facts_fts MATCH ? "
            "ORDER BY ft.rank LIMIT 6",
            (quoted,),
        ).fetchall()
        conn.close()
    except Exception as e:
        log.warning("memory: fact_store FTS query failed: %s", e)
        return ""
    if not rows:
        return ""
    out = ["## fact_store — operational facts matching your query\n"]
    for r in rows:
        out.append(f"- [id {r['fact_id']} | {r['category']}] {r['snippet']}")
    text = "\n".join(out)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[…truncated…]"
    return text


def _read_life_memory_for_query(query: str, max_chars: int = MAX_LIFE_CHARS) -> str:
    """Pull relevant life-memory files for the current query.

    Two strategies, applied in order:

    1. **Keyword index** — if MEMORY_INDEX_PATH is a flat "keyword: path"
       file (the original spec), match by exact keyword. Cheap and explicit.

    2. **Recursive content search** — if that yields nothing AND the
       query has substantive tokens, walk LIFE_MEMORY_DIR for *.md files
       whose content shares tokens with the query. This handles real-world
       memories where the index is descriptive rather than keyword-mapped
       (LIFE_MEMORY/INDEX.md is a table of contents, not a key→file map).

    Capped at max_chars so a hot keyword doesn't blow the system prompt.
    """
    if not LONG_TERM_MEMORY_DIR.exists():
        return ""
    out: list[str] = []
    files: list[Path] = []

    # Strategy 1: keyword index
    if SAM_INDEX_PATH.exists():
        try:
            q_tokens = set(query.lower().split())
            for line in SAM_INDEX_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    kw, rel = line.split(":", 1)
                    if kw.lower() in q_tokens:
                        f = (LONG_TERM_MEMORY_DIR / rel.strip()).resolve()
                        try:
                            f.relative_to(LONG_TERM_MEMORY_DIR.resolve())
                        except ValueError:
                            continue
                        if f.exists() and f.is_file():
                            files.append(f)
                if len(files) >= 4:
                    break
        except Exception as e:
            log.warning("memory: keyword index parse failed: %s", e)

    # Strategy 2: recursive token match (only if strategy 1 didn't hit)
    if not files:
        stop = {
            "the", "and", "for", "are", "but", "not", "you", "all", "can", "had",
            "her", "was", "one", "our", "out", "day", "get", "has", "him", "his",
            "how", "its", "may", "new", "now", "old", "see", "way", "who", "boy",
            "did", "let", "say", "she", "too", "use", "what", "when", "your",
            "from", "this", "that", "with", "have", "will", "just", "tell",
            "about", "know", "they", "them", "than", "then", "there", "their",
        }
        tokens = [
            t.lower() for t in re.findall(r"[A-Za-z0-9_]+", query)
            if len(t) >= 4 and t.lower() not in stop
        ]
        if tokens:
            token_set = set(tokens)
            candidates: list[tuple[int, Path]] = []
            try:
                for md in LONG_TERM_MEMORY_DIR.rglob("*.md"):
                    if not md.is_file():
                        continue
                    try:
                        text = md.read_text(encoding="utf-8", errors="replace").lower()
                    except Exception:
                        continue
                    score = sum(1 for tok in token_set if tok in text)
                    if score > 0:
                        candidates.append((score, md))
            except Exception as e:
                log.warning("memory: recursive life-memory walk failed: %s", e)
            candidates.sort(key=lambda x: -x[0])
            files = [p for _, p in candidates[:3]]

    # Boost: if the query contains identity keywords and an identity file
    # exists, hoist it to the front so personal-fact queries always get the
    # canonical identity file even when token scoring ties with unrelated hits.
    #
    # The identity file is whatever MEMORY_IDENTITY_FILE points at (a path
    # or a bare filename), defaulting to a generic name. Nothing here is
    # hardcoded to a particular person.
    identity_keywords = {"birthday", "born", "name", "wife", "kids", "children",
                         "son", "daughter", "family", "address", "phone",
                         "email", "work", "job", "callsign"}
    if any(tok in identity_keywords for tok in token_set):
        _id_name = os.environ.get("MEMORY_IDENTITY_FILE", "identity.md").strip()
        _id_names = {_id_name.lower()}
        if "/" in _id_name:
            _id_names.add(_id_name.split("/")[-1].lower())
        identity_candidates = [
            (s, p) for s, p in candidates
            if p.name.lower() in _id_names
        ]
        if identity_candidates:
            identity_file = identity_candidates[0][1]
            files = [identity_file] + [f for f in files if f != identity_file]
            files = files[:3]

    if not files:
        return ""

    out.append("## long-term memory — relevant context\n")
    remaining = max_chars
    for f in files[:3]:
        chunk_size = min(remaining, 1500)
        text = _safe_read(f, chunk_size)
        if text:
            try:
                rel = f.relative_to(LONG_TERM_MEMORY_DIR)
            except ValueError:
                rel = f
            out.append(f"\n### {rel}\n{text}\n")
            remaining -= len(text)
            if remaining <= 100:
                break
    return "\n".join(out).strip()


# ---- Public entry point ----

def build_memory_context(user_text: str) -> str:
    """Build the memory block to inject into the system prompt.

    Order: SOUL.md (identity) → index → FTS facts → long-term memory.
    Each layer is optional and capped. Returns an empty string if no
    memory sources are configured — the bridge still works without
    memory, it just won't have identity/context.
    """
    parts: list[str] = []
    soul = _safe_read(SOUL_PATH, MAX_SOUL_CHARS)
    if soul:
        parts.append(f"## SOUL.md — your identity & voice rules\n{soul}\n")
    index = _safe_read(SAM_INDEX_PATH, MAX_INDEX_CHARS)
    if index:
        parts.append(f"## MEMORY_INDEX.md — your memory index\n{index}\n")
    facts = _read_fact_store_for_query(user_text)
    if facts:
        parts.append(facts + "\n")
    life = _read_life_memory_for_query(user_text)
    if life:
        parts.append(life + "\n")
    return "\n".join(parts).strip()


# ---- Session history (disk-persisted) ----

_SESSIONS_LOCK = threading.Lock()
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _session_path(session_id: str) -> Path:
    # Sanitize session_id (it comes from the client) — allow only safe chars
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", session_id)[:64]
    return SESSIONS_DIR / f"{safe}.jsonl"


_POISONED_ASSISTANT_PATTERNS = re.compile(
    r"(i (don'?t|do not) (have|know)|"
    r"memory (errors?|unavailable|system .* error)|"
    r"still throwing errors?|"
    r"can'?t (access|confirm) (your )?(birthday|memory|info)|"
    r"my (memory|fact store) (is|are) (throwing|broken|failing|empty)|"
    r"not (going to )?make (one|it) up|"
    r"i (asked|never got|don'?t want to guess))",
    re.IGNORECASE,
)


def _is_poisoned_assistant(content: str) -> bool:
    """True if this assistant turn claims a memory failure that the model
    might pattern-match in subsequent turns. We drop these from history so
    a recovered memory layer isn't shadowed by stale "I don't have access"
    replies from earlier in the same session."""
    if not content or len(content) > 1500:
        return False
    matches = _POISONED_ASSISTANT_PATTERNS.findall(content)
    return len(matches) >= 2  # need ≥2 distinct poison phrases to drop


def load_history(session_id: str, max_turns: int = 10) -> list[dict]:
    """Load persisted history for a session. Returns list of {role, content}.

    Filters out assistant turns that look like "memory unavailable" replies —
    these were true at the time but poison future turns by teaching the
    model to pattern-match its own stale failures.
    """
    path = _session_path(session_id)
    if not path.exists():
        return []
    out: list[dict] = []
    raw: list[dict] = []
    try:
        with _SESSIONS_LOCK:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw.append(__import__("json").loads(line))
                    except Exception:
                        continue
    except Exception as e:
        log.warning("memory: failed to load history for %s: %s", session_id, e)
        return []
    # Drop poisoned assistant turns (keep user turns so context isn't lost).
    dropped = 0
    for entry in raw:
        if entry.get("role") == "assistant" and _is_poisoned_assistant(entry.get("content", "")):
            dropped += 1
            continue
        out.append(entry)
    if dropped:
        log.info("memory: dropped %d poisoned 'no-access' assistant turn(s) from %s history", dropped, session_id)
    # Each turn = 1 user + 1 assistant; keep last max_turns * 2
    return out[-(max_turns * 2):]


def append_turn(session_id: str, role: str, content: str) -> None:
    """Append one turn to the on-disk session log (newline-delimited JSON)."""
    import json
    path = _session_path(session_id)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "role": role,
        "content": content,
    }
    try:
        with _SESSIONS_LOCK:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("memory: failed to persist turn for %s: %s", session_id, e)


def clear_session(session_id: str) -> None:
    path = _session_path(session_id)
    try:
        path.unlink(missing_ok=True)
    except Exception as e:
        log.warning("memory: failed to clear session for %s: %s", session_id, e)


def session_count() -> int:
    """How many sessions on disk. Useful for diagnostics."""
    return len(list(SESSIONS_DIR.glob("*.jsonl")))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    print("=== ava.memory: placeholder memory layer ===")
    print(f"  SOUL_PATH     = {SOUL_PATH} (exists: {SOUL_PATH.exists()})")
    print(f"  INDEX_PATH    = {SAM_INDEX_PATH} (exists: {SAM_INDEX_PATH.exists()})")
    print(f"  LONG_TERM_DIR  = {LONG_TERM_MEMORY_DIR} (exists: {LONG_TERM_MEMORY_DIR.exists()})")
    print(f"  FACT_STORE_DB = {FACT_STORE_DB} (exists: {FACT_STORE_DB.exists()})")
    print(f"  SESSIONS_DIR  = {SESSIONS_DIR} (exists: {SESSIONS_DIR.exists()})")
    print()
    print("To wire in real memory:")
    print("  1. Set MEMORY_SOUL_PATH to your agent's voice/personality markdown")
    print("  2. Set MEMORY_INDEX_PATH to a file of 'keyword: path.md' lines")
    print("  3. Populate MEMORY_LIFE_DIR with the referenced files")
    print()
    ctx = build_memory_context("hello")
    print(f"  build_memory_context('hello') → {len(ctx)} chars (empty if no config)")