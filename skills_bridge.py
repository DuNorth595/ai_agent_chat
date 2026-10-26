# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""
skills_bridge.py — Skill loader for the voice bridge.

Loads SKILL.md files relevant to the current query and injects them
into the system prompt. Two strategies:

1. ALWAYS-LOAD: skills present on every turn. Configurable via the
   `AVA_ALWAYS_LOAD_SKILLS` env var (comma-separated names). Default:
   empty.
2. TRIGGER-LOAD: skills surfaced by keyword heuristics. The default
   trigger map below is intentionally tiny and generic — operators
   should fork this file and add their own keyword → skill mappings.

Each loaded skill is summarized in the prompt as: name + a short
description + a brief excerpt of the procedure. The full text is NOT
inlined — too big. If the model needs more, it can call a `read_file`
tool to fetch the full SKILL.md.

Source paths:
  $AVA_SKILLS_ROOT  default: ~/.config/ai_agent_chat/skills

A skill is a directory `<root>/<name>/SKILL.md`. Frontmatter (YAML
between `---` lines) is parsed for `name` + `description`.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Iterable

log = logging.getLogger("ava.skills")

# Configurable. Default: ~/.config/ai_agent_chat/skills
def _read_bridge_env() -> dict:
    """Read the bridge's own .env (if present) without requiring dotenv."""
    env: dict = {}
    env_file = Path(os.environ.get("AVA_ENV_FILE") or (Path(__file__).resolve().parent / ".env"))
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


SKILLS_ROOT = Path(
    os.environ.get("AVA_SKILLS_ROOT")
    or _read_bridge_env().get("AVA_SKILLS_ROOT")
    or (Path.home() / ".config" / "ai_agent_chat" / "skills")
)

# Configurable. Default: empty list. Operators set this to whatever
# skills they want present on every turn, comma-separated.
_ALWAYS_LOAD_ENV = os.environ.get("AVA_ALWAYS_LOAD_SKILLS", "")
ALWAYS_LOAD: tuple[str, ...] = tuple(
    s.strip() for s in _ALWAYS_LOAD_ENV.split(",") if s.strip()
)


# Default trigger keyword → skill name mapping.
#
# This list is intentionally tiny and generic — operators should fork
# this file and add their own mappings. The defaults here cover the
# most common voice-bridge maintenance topics (TTS/STT provider swaps,
# audio quirks, deployment hygiene). Personal/callsign/project triggers
# belong in the operator's fork, not here.
DEFAULT_TRIGGER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "tts": ("voice-widget-piper-swap",),
    "piper": ("voice-widget-piper-swap",),
    "stt": ("voice-widget-piper-swap",),
    "whisper": ("voice-widget-piper-swap",),
    "audio cut": ("voice-widget-first-word-cutoff",),
    "first word": ("voice-widget-first-word-cutoff",),
    "browser": ("browser-tool-stability-fix",),
    "launchd": ("launchagent-env-drift",),
    "launchagent": ("launchagent-env-drift",),
    "cron": ("hermes-cron-script-args",),
    "telegram bot": ("intel-brief-telegram-token-rotation",),
    "token rotation": ("intel-brief-telegram-token-rotation",),
    "memory": ("saving-cross-session-memory-context",),
    "fact_store": ("saving-cross-session-memory-context",),
}

# Operator-overridable. If AVA_TRIGGER_KEYWORDS_JSON is set, it replaces
# DEFAULT_TRIGGER_KEYWORDS entirely. Use this to plug in your own map
# without forking the file.
_TRIGGER_OVERRIDE = os.environ.get("AVA_TRIGGER_KEYWORDS_JSON")
if _TRIGGER_OVERRIDE:
    import json
    try:
        TRIGGER_KEYWORDS: dict[str, tuple[str, ...]] = {
            k: tuple(v) for k, v in json.loads(_TRIGGER_OVERRIDE).items()
        }
    except Exception as e:
        log.warning("AVA_TRIGGER_KEYWORDS_JSON parse failed: %s; using defaults", e)
        TRIGGER_KEYWORDS = DEFAULT_TRIGGER_KEYWORDS
else:
    TRIGGER_KEYWORDS = DEFAULT_TRIGGER_KEYWORDS


def _read_skill(name: str) -> str | None:
    path = SKILLS_ROOT / name / "SKILL.md"
    if not path.exists():
        return None
    try:
        return path.read_text()
    except OSError as e:
        log.warning("failed to read skill %s: %s", name, e)
        return None


def _parse_frontmatter(text: str) -> tuple[str, str]:
    """Returns (name, description) from YAML frontmatter.

    Handles multi-line YAML descriptions with literal block style (|).
    """
    if not text.startswith("---"):
        return ("", "")
    end = text.find("\n---", 3)
    if end < 0:
        return ("", "")
    fm = text[3:end].strip()
    name = ""
    description = ""
    desc_buf: list[str] = []
    in_description = False
    for line in fm.splitlines():
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip()
            in_description = False
        elif line.startswith("description:"):
            rest = line.split(":", 1)[1].strip()
            if rest.startswith("|") or rest.startswith(">"):
                in_description = True
                desc_buf = []
            else:
                description = rest
                in_description = False
        elif in_description:
            # Continuation of a block-style description
            desc_buf.append(line)
    if desc_buf and not description:
        description = "\n".join(desc_buf).strip()
    return (name, description)


def _summarize(name: str, text: str, max_chars: int = 600) -> str:
    """Build a compact summary: name + description + brief excerpt."""
    _, desc = _parse_frontmatter(text)
    body_lines = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.startswith("---")
    ]
    excerpt = "\n".join(body_lines[:6])
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars] + "\n[…truncated…]"
    return f"- **{name}** — {desc}\n  ```\n  {excerpt}\n  ```"


def load_skills_for_query(query: str, always_load: Iterable[str] | None = None) -> str:
    """Return a markdown block summarizing relevant skills.

    Combines always-load skills + trigger-matched skills (deduped), capped
    at 4 total. Returns an empty string if no skills are configured or
    none match.
    """
    always_load = tuple(always_load) if always_load is not None else ALWAYS_LOAD
    q = query.lower()
    triggered: list[str] = []
    for kw, names in TRIGGER_KEYWORDS.items():
        if kw in q:
            triggered.extend(names)
    seen: set[str] = set()
    ordered: list[str] = []
    for s in list(always_load) + triggered:
        if s and s not in seen:
            seen.add(s)
            ordered.append(s)
    ordered = ordered[:4]

    blocks: list[str] = []
    for name in ordered:
        text = _read_skill(name)
        if not text:
            continue
        blocks.append(_summarize(name, text))

    if not blocks:
        return ""

    header = "## Active skills\n"
    note = (
        "(Procedures for this query — each is summarized here. Use the\n"
        "`read_file` tool to fetch the full SKILL.md if needed.)\n"
    )
    return header + note + "\n".join(blocks)


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    print(f"=== skills_bridge: skills root = {SKILLS_ROOT} ===")
    print(f"    exists: {SKILLS_ROOT.exists()}")
    print(f"    always-load: {ALWAYS_LOAD}")
    print(f"    trigger keywords: {len(TRIGGER_KEYWORDS)}")
    print()
    ctx = load_skills_for_query("how do I configure piper?")
    print(f"--- load_skills_for_query('how do I configure piper?') ---")
    print(ctx if ctx else "(empty — no skills configured)")