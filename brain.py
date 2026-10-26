#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""
brain.py — the voice bridge's brain (LLM agent loop).

Calls an Anthropic-compatible chat completions API and streams text
responses so TTS can start speaking before the full reply is done.
The default model is `MINIMAX_M3` via `api.minimax.io`; set
`BRAIN_API_URL` and `BRAIN_MODEL` to point at any other Anthropic-
compatible endpoint.

Memory-aware: every call gets SOUL.md + memory index + relevant
fact_store facts + long-term memory context injected into the system
prompt (see `memory.py`).

Auth: `BRAIN_API_KEY` env var (e.g. `MINIMAX_API_KEY`).
Conversation memory: persisted to disk by `memory.py` (survives restarts).
Sessions identified by a client-provided session_id.
"""
import os
import re
import time
import json
import logging
import uuid
from pathlib import Path
from typing import Iterator

import requests
import memory  # the new memory layer
import tools  # the tool registry + implementations
import skills_bridge  # SKILL.md loader (always-load + trigger-load)
import telegram_sync  # recent Telegram context (home + DMs)

log = logging.getLogger("ava.brain")

API_URL = os.environ.get("BRAIN_API_URL", "https://api.minimax.io/anthropic/v1/messages")
DEFAULT_MODEL = os.environ.get("BRAIN_MODEL", "MINIMAX-M3")
# Voice needs enough room to develop a thought without rambling. 600 tokens
# is ~450 spoken words — enough for a full answer with texture, not so much
# that the model monologues.
# v0.10.1 — MAX_TOKENS was a hardcoded 600, which long answers
# hit routinely. When the model stopped at max_tokens AND had
# pending tool_use blocks, the agent loop CONTINUED and re-sent the
# same messages — producing a second completion that restarted the
# answer from the top. The user saw the reply begin, restart, and
# repeat itself. (The model even noticed: "Same as what I just told
# you" / "You asked twice already.")
#
# Two fixes: (a) raise the cap and make it env-tunable, and
# (b) never re-loop on max_tokens — see the stop_reason branch in
# stream_reply.
MAX_TOKENS = int(os.environ.get("BRAIN_MAX_TOKENS", "2000"))

# Confirmation protocol: when the voice agent wants to use an EXTERNAL_WRITE tool
# (send_message), it MUST describe the action and end the turn asking for
# "do it" / "cancel". We hold the pending action in a per-session store so
# the NEXT turn can resolve it.
#
# Lifecycle:
#   pending_confirm[session_id] = {"tool": name, "args": {...}, "summary": str}
#
# On user reply, brain checks if the user said "do it" / "yes" / "send it"
# → executes the held tool call. Or "cancel" / "never mind" → drops it.
# Anything else → treats the reply as a new turn.
#
# This is what makes the voice bridge trustworthy. Without it, "I'll send the
# Telegram" could fire without a real confirmation. With it, the words "I
# will" don't mean done — they mean "I'm asking, hold on."

# Voice-mode overlay — ADDED ON TOP of SOUL.md, not a replacement for it.
# SOUL.md (loaded via memory.build_memory_context) carries the agent's actual
# personality, opinions, voice, and relationship with the user. This overlay
# only adds the constraints specific to speaking out loud. Do NOT paste a
# "customer service bot" script over the top of it — that flattens the
# personality.
VOICE_OVERLAY = """--- VOICE MODE OVERLAY ---

You're speaking out loud through a TTS pipeline (Piper, libritts_r voice by default).
The MEMORY CONTEXT above establishes who you are, your personality, and your
relationship with the user. Don't replace that with a generic voice-bot persona
— *be the agent*, just through audio.

Voice-specific constraints (the ONLY things that change vs. text mode):

A. **Numbers and symbols** — spell them out so they survive TTS:
   - Numbers as words ("twenty-three" not "23", "nineteen eighty-two" not "1982") UNLESS the user asks for a specific code/identifier (callsigns, IPs, port numbers, file paths). For those, spell them out phonetically: "one two seven dot zero dot zero point one" not "127.0.0.1".
   - "@" → "at", "." in URLs/paths → "dot", "&" → "and", "%" → "percent", "$" → "dollar", "#" → "hash", "_" → "underscore", "/" → "slash".
   - Currency: spell out "three thousand dollars" not "$3,000".
   - Email/URLs: never read them — say you'll send or show instead.

B. **Don't show, tell.** No markdown, no bullets, no code blocks, no JSON in replies. If a structured answer is unavoidable, weave it into prose ("there are three: X, Y, and Z") instead of formatting.

C. **Address the user by name occasionally**, but not every turn — only when it lands. Don't start every reply with their name.

D. **Stop words the user says "stop", "shut up", "quiet", or goes silent for too long.** Just stop talking. Don't apologize, don't ask if they're done, don't continue with a wrap-up.

E. **If you don't know, say so.** If the memory doesn't cover a question, say "let me check" or "I don't have that pulled up" — never guess. If you're about to invent a fact, stop.

F. **Humor and opinions stay.** Dry wit, callbacks to past conversations, opinions on projects, gentle ribbing — all of that survives voice. A short reply can still have personality ("Hey. What's up?").

G. **Length:** match the question. A factual question gets a short factual answer. A "tell me about my family" gets a fuller rundown with the texture SOUL.md would normally give it. The voice agent isn't shorter than the text agent — it's just speech-shaped.

|H. **Tools and execution.** You have real tools available:
   - `search_files`, `read_file`, `web_fetch` — read-only, fire freely.
   - `fact_store_add`, `cron_create`, `delegate_task` — local writes, fire and report.
   - `send_message` (Telegram) — IRREVERSIBLE EXTERNAL WRITE. CRITICAL: When the user says "don't confirm," "just send it," "fire away," "no need to ask," "skip confirmation," or anything similar, you MUST STILL call the `send_message` tool. The bridge detects those phrases and auto-confirms the tool call — you don't need to do anything different. Call the tool in the same turn; the bridge handles the rest. NEVER narrate "sent" without a tool call — narration is not execution. If the user has not said any of those pre-approval phrases, the default flow is: (a) describe the action out loud, (b) actually CALL the `send_message` tool in the same turn. The bridge will hold the action and wait for the user to say "do it" or "cancel" on the next turn. The tool result will tell you whether it was held or auto-confirmed.

|I. **Skills and cross-surface context are already loaded above.** The system prompt may include relevant skill summaries and recent channel context. Treat them as read-only awareness — don't quote them verbatim to the user, but use them to inform your answer.

You're the agent. Memory, personality, relationship, opinions — all yours. The overlay above is just the speech codec.

--- END VOICE MODE OVERLAY ---
"""


# Extra env files searched for a key, in order. The first two are
# conventional; the rest let an operator point at a shared secrets file
# without hardcoding anyone's home directory here.
KEY_ENV_FILES = (
    "$AVA_ENV_FILE",
    "./.env",
    "~/.config/ai_agent_chat/.env",
    "~/.hermes/.env",
)


def _read_key_from(path: Path) -> str:
    """Return the first BRAIN_API_KEY / MINIMAX_API_KEY found in a file."""
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith(("BRAIN_API_KEY=", "MINIMAX_API_KEY=")):
                return line.split("=", 1)[1].strip().strip("'\"")
    except Exception:
        pass
    return ""


def get_api_key() -> str:
    """Read BRAIN_API_KEY from the environment, then from an env file.

    Searched in order: the process environment, then each file in
    KEY_ENV_FILES. $AVA_ENV_FILE overrides the list entirely.
    """
    key = os.environ.get("BRAIN_API_KEY") or os.environ.get("MINIMAX_API_KEY")
    if not key:
        override = os.environ.get("AVA_ENV_FILE")
        candidates = [override] if override else list(KEY_ENV_FILES)
        for raw in candidates:
            if not raw:
                continue
            path = Path(os.path.expanduser(raw))
            if not path.is_absolute():
                path = Path(__file__).resolve().parent / path
            key = _read_key_from(path)
            if key:
                break
    if not key:
        raise RuntimeError(
            "BRAIN_API_KEY (or MINIMAX_API_KEY) not set in env or $AVA_ENV_FILE"
        )
    return key


def _build_system_prompt(user_text: str) -> str:
    """Build the full system prompt with memory context for this turn.

    Order:
      1. SOUL/personality + life_memory + fact_store (memory.build_memory_context)
      2. Active Hermes skills (skills_bridge) — same patterns I use in text
      3. Recent Telegram context (telegram_sync) — what's happening elsewhere
      4. Voice-mode overlay (LAST, so TTS constraints are the most recent
         instruction but personality remains the foundation).

    SOUL is first because it grounds identity; voice overlay is last because
    it shapes the speech codec without overwriting the personality. Skills
    and Telegram context sit in the middle as operational awareness.
    """
    parts: list[str] = []

    # 1. SOUL + memory
    try:
        ctx = memory.build_memory_context(user_text)
    except Exception as e:
        log.warning("brain: memory context build failed: %s", e)
        ctx = "(memory layer unavailable)"
    if ctx:
        parts.append(ctx)
    else:
        parts.append(
            "You are a voice assistant. "
            "Memory/context is unavailable for this turn — say so honestly "
            "rather than guessing."
        )

    # 2. Skills — relevant to this turn
    try:
        skills_ctx = skills_bridge.load_skills_for_query(user_text)
        if skills_ctx:
            parts.append(skills_ctx)
    except Exception as e:
        log.warning("brain: skills context build failed: %s", e)

    # 3. Telegram context — what's been happening on Telegram
    try:
        tg_ctx = telegram_sync.get_full_context(limit_chars=1500)
        if tg_ctx:
            parts.append("### Live cross-surface context (read-only awareness)\n" + tg_ctx)
    except Exception as e:
        log.warning("brain: telegram context build failed: %s", e)

    # 4. Voice overlay (always last — shapes the codec, not the personality)
    parts.append(VOICE_OVERLAY)

    return "\n\n".join(parts)


# In-memory store of pending confirmation requests. Keyed by session_id.
# Populated when voice Sam wants to call an EXTERNAL_WRITE tool. Consumed
# (or expired) on the next user reply. NOT persisted to disk — if the
# bridge restarts mid-confirmation, the held action is dropped, which is
# the safer default.
_PENDING_CONFIRM: dict[str, dict] = {}


def set_pending_confirm(session_id: str, payload: dict | None) -> None:
    """Hold (or clear) a pending confirmation request for this session."""
    if payload is None:
        _PENDING_CONFIRM.pop(session_id, None)
    else:
        _PENDING_CONFIRM[session_id] = payload


def get_pending_confirm(session_id: str) -> dict | None:
    return _PENDING_CONFIRM.get(session_id)


# Affirmative/negative phrases the user might say to resolve a pending
# confirmation. Match is case-insensitive, anchored to short strings.
_CONFIRM_YES = re.compile(
    r"^(yes|yeah|yep|yup|sure|do it|send it|send|go|go ahead|fire|fire it|confirmed?|affirmative|please do|ok do it)\b",
    re.IGNORECASE,
)
_CONFIRM_NO = re.compile(
    r"^(no|nope|nah|cancel|stop|never mind|nevermind|don'?t|hold|wait|not now|skip)\b",
    re.IGNORECASE,
)

# Pre-approval phrases — when present in the user's *current* message that
# triggered an external_write tool call, the call is auto-confirmed (no
# hold + wait). Match is substring, case-insensitive.
_PRE_APPROVE = re.compile(
    r"(no need to (ask|confirm)|don'?t (need to )?confirm|skip (the )?confirm|"
    r"just (send|go ahead|fire|do it)|fire (it |away)|"
    r"no confirmation|don'?t ask|without (asking|confirming)|"
    r"no confirm(ation)? needed|send it (anyway|without)|"
    r"go (ahead|for it) (and )?send|approved)",
    re.IGNORECASE,
)


def _looks_like_confirmation(text: str) -> str | None:
    """Return 'yes' | 'no' | None based on whether the reply resolves a pending action."""
    t = text.strip()
    if not t:
        return None
    if _CONFIRM_YES.match(t):
        return "yes"
    if _CONFIRM_NO.match(t):
        return "no"
    return None


def _has_pre_approval(text: str) -> bool:
    """True if the user's message pre-approves the next external write."""
    return bool(text and _PRE_APPROVE.search(text))


# Persistent log of every tool call voice Sam has made. Keyed by session_id
# so /api/debug can show the trail. Bounded per session.
_TOOL_LOG: dict[str, list[dict]] = {}
_TOOL_LOG_MAX = 100


def _log_tool_call(session_id: str, name: str, args: dict, result: dict, tier: str) -> None:
    entry = {
        "ts": time.time(),
        "tool": name,
        "tier": tier,
        "args": args,
        "ok": "error" not in result,
        "result_summary": _summarize_result(name, result),
    }
    log_list = _TOOL_LOG.setdefault(session_id, [])
    log_list.append(entry)
    if len(log_list) > _TOOL_LOG_MAX:
        del log_list[: len(log_list) - _TOOL_LOG_MAX]
    log.info("tool[%s]: %s args=%s → %s", tier, name, _clip(args), entry["result_summary"])


def _summarize_result(name: str, result: dict) -> str:
    if "error" in result:
        return f"ERROR: {result['error'][:120]}"
    if name == "send_message" and result.get("ok"):
        return f"message_id={result.get('message_id')} chat={result.get('chat_id')}"
    if name == "fact_store_add" and result.get("ok"):
        return f"fact_id={result.get('fact_id')} category={result.get('category')}"
    if name == "cron_create" and result.get("ok"):
        return f"name={result.get('name')} deliver={result.get('deliver')}"
    if name == "search_files":
        return f"{result.get('count', 0)} matches"
    if name == "read_file":
        return f"{result.get('total_lines', 0)} lines, truncated={result.get('truncated', False)}"
    if name == "web_fetch":
        return f"HTTP {result.get('status', '?')} bytes={len(result.get('body', ''))}"
    return json.dumps(result)[:120]


def _clip(obj, n: int = 200) -> str:
    s = json.dumps(obj) if not isinstance(obj, str) else obj
    return s if len(s) <= n else s[:n] + "..."


def get_tool_log(session_id: str) -> list[dict]:
    return list(_TOOL_LOG.get(session_id, []))


# Cap how many tool-use loops we'll do per turn — prevents infinite loops
# where the model keeps calling tools without producing a final answer.
_MAX_TOOL_LOOPS = 5


def stream_reply(
    user_text: str,
    history: list[dict] | None = None,
    session_id: str | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = MAX_TOKENS,
) -> Iterator[dict]:
    """Agent loop. Yields structured events, not raw text strings.

    Event shapes yielded:
      - {"type": "text", "delta": "..."} — visible to user; TTS should speak.
      - {"type": "tool_call", "name": "...", "args": {...}, "tier": "..."} —
        announced in logs; user can see in /api/debug.
      - {"type": "tool_result", "name": "...", "ok": bool, "summary": "..."} —
        follow-up to a tool_call.
      - {"type": "pending_confirm", "tool": "...", "args": {...}, "summary": "..."} —
        voice Sam wants to do an irreversible action; user must say "do it".
      - {"type": "done", "stop_reason": "..."} — turn complete.

    history: list of {"role": "user"|"assistant", "content": "..."} prior turns.
    session_id: required if you want pending_confirm + tool_log to work.

    Backward-compatible: yields text deltas first, then tool events. The
    text events are what /api/chat will route to TTS.
    """
    sid = session_id or "anon"
    # v0.10.1 — per-call identity so we can tell one stream_reply from
    # two. When a reply "restarts", the log shows either a second
    # "brain: call <id>" (two calls) or the same id twice (one call
    # looping).
    _call_id = uuid.uuid4().hex[:6]
    log.info("brain: call %s start (session=%s)", _call_id, sid)

    messages = list(history or [])
    messages.append({"role": "user", "content": user_text})

    api_key = get_api_key()
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "X-Api-Key": api_key,
    }
    system_prompt = _build_system_prompt(user_text)

    # v0.8: Anthropic prompt caching — wrap the system prompt as a list with
    # an ephemeral cache_control breakpoint. The first ~6000 chars of system
    # prompt (SOUL/personality/memory/skills/telegram context) are identical
    # across turns, so caching them server-side shaves first-token latency
    # from ~600ms to ~150ms. ephemeral = 5min TTL, perfect for back-to-back
    # conversation turns.
    system_blocks = [
            {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
        ]

    for loop_iter in range(_MAX_TOOL_LOOPS + 1):
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "stream": True,
            "system": system_blocks,
            "messages": messages,
            "tools": tools.anthropic_tools(),
        }

        # Streaming response parser. Anthropic streams events in this order:
        #   message_start → content_block_start (text or tool_use) → deltas
        #   → content_block_stop → ... → message_stop
        # We accumulate text and tool_use blocks, then route on stop_reason.
        text_chunks: list[str] = []
        tool_uses: list[dict] = []  # [{id, name, input}]
        stop_reason: str | None = None
        current_tool: dict | None = None
        current_tool_input_buf: list[str] = []

        t0 = time.time()
        try:
            with requests.post(API_URL, json=body, headers=headers, stream=True, timeout=60) as r:
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data: "):
                        continue
                    raw = line[6:]
                    if raw.strip() == "[DONE]":
                        break
                    try:
                        evt = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    etype = evt.get("type")

                    if etype == "message_start":
                        # v0.8: log cache hit info from message_start.message.usage
                        usage = (evt.get("message") or {}).get("usage") or {}
                        if usage:
                            cache_read = usage.get("cache_read_input_tokens", 0)
                            cache_create = usage.get("cache_creation_input_tokens", 0)
                            if cache_read or cache_create:
                                log.info(
                                    "brain cache: read=%d created=%d input=%d",
                                    cache_read, cache_create, usage.get("input_tokens", 0),
                                )
                        continue  # don't yield this internal event to consumers

                    if etype == "content_block_start":
                        cb = evt.get("content_block", {})
                        if cb.get("type") == "tool_use":
                            current_tool = {
                                "id": cb.get("id"),
                                "name": cb.get("name"),
                                "input": {},
                            }
                            current_tool_input_buf = []

                    elif etype == "content_block_delta":
                        delta = evt.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                text_chunks.append(text)
                                yield {"type": "text", "delta": text}
                        elif delta.get("type") == "input_json_delta" and current_tool is not None:
                            current_tool_input_buf.append(delta.get("partial_json", ""))

                    elif etype == "content_block_stop":
                        if current_tool is not None:
                            raw_json = "".join(current_tool_input_buf) or "{}"
                            try:
                                current_tool["input"] = json.loads(raw_json)
                            except json.JSONDecodeError:
                                current_tool["input"] = {"_raw": raw_json[:500]}
                            tool_uses.append(current_tool)
                            current_tool = None
                            current_tool_input_buf = []

                    elif etype == "message_delta":
                        stop_reason = evt.get("delta", {}).get("stop_reason") or stop_reason

                    elif etype == "message_stop":
                        break
        except Exception as e:
            log.exception("brain: API call failed")
            # Yield a SHORT error sentinel — NOT the full exception text.
            # Long exception strings were getting saved into session history
            # and poisoning future turns (the model would imitate the error
            # string verbatim). Keep this terse so the model has a clean slate
            # next time around.
            yield {"type": "text", "delta": "I hit a brain error. Try again in a moment."}
            yield {"type": "done", "stop_reason": "error"}
            return

        log.info(
            "brain: call %s loop %d — %d text chunks, %d tool_use blocks, stop_reason=%s (%.2fs)",
            _call_id, loop_iter, len(text_chunks), len(tool_uses),
            stop_reason, time.time() - t0,
        )

        # v0.10.1 — If we hit max_tokens, STOP. Do not re-loop.
        #
        # The old code fell through to the tool-execution branch, which
        # appended the assistant message and re-sent the same request.
        # The model then produced a SECOND completion from scratch, and
        # because the first completion's text had already been streamed
        # to the client, the reply visibly restarted and repeated
        # itself. Partial text is strictly better than duplicated text.
        if stop_reason == "max_tokens":
            log.warning(
                "brain: hit max_tokens (%d) on loop %d — stopping, "
                "not re-looping", max_tokens, loop_iter,
            )
            yield {"type": "done", "stop_reason": "max_tokens"}
            return

        # If the model finished without tool calls, we're done.
        if not tool_uses:
            yield {"type": "done", "stop_reason": stop_reason or "end_turn"}
            return

        # Otherwise: run each tool, classify tiers, handle the EXTERNAL_WRITE
        # ones via pending store, and continue the loop.
        # We need to send the assistant's full message back (text + tool_use)
        # plus a tool_result per tool. Reconstruct the assistant content.
        assistant_content: list[dict] = []
        # Re-emit any text that was streamed (already yielded above, but the
        # API needs the full content block in the assistant turn).
        full_text = "".join(text_chunks)
        if full_text:
            assistant_content.append({"type": "text", "text": full_text})
        for tu in tool_uses:
            assistant_content.append({
                "type": "tool_use",
                "id": tu["id"],
                "name": tu["name"],
                "input": tu["input"],
            })
        messages.append({"role": "assistant", "content": assistant_content})

        # Run each tool. EXTERNAL_WRITE: hold instead of execute, return
        # "pending_confirm" result so the model narrates and waits.
        tool_results: list[dict] = []
        held_external = False
        for tu in tool_uses:
            name = tu["name"]
            args = tu["input"]
            tier = tools.tier_of(name) or "external_write"
            yield {"type": "tool_call", "name": name, "args": args, "tier": tier}

            if tier == tools.EXTERNAL_WRITE:
                # If they pre-approved in the same message, run it now.
                if _has_pre_approval(user_text):
                    result = tools.execute(name, args)
                    _log_tool_call(sid, name, args, result, tier)
                    yield {"type": "tool_result", "name": name, "ok": "error" not in result, "summary": _summarize_result(name, result)}
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": json.dumps(result) if "error" not in result else json.dumps(result),
                        "is_error": "error" in result,
                    })
                    yield {"type": "auto_confirmed", "tool": name, "args": args, "summary": _describe_external(name, args)}
                    continue
                # Hold for confirmation. Don't run it.
                summary = _describe_external(name, args)
                set_pending_confirm(sid, {"tool": name, "args": args, "summary": summary})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": json.dumps({
                        "held": True,
                        "reason": "external_write_requires_confirmation",
                        "summary": summary,
                    }),
                    "is_error": False,
                })
                held_external = True
                yield {"type": "pending_confirm", "tool": name, "args": args, "summary": summary}
            else:
                result = tools.execute(name, args)
                _log_tool_call(sid, name, args, result, tier)
                yield {
                    "type": "tool_result",
                    "name": name,
                    "ok": "error" not in result,
                    "summary": _summarize_result(name, result),
                }
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": json.dumps(result),
                    "is_error": "error" in result,
                })

        if held_external:
            # Append the tool results so the model sees them and narrates
            # the hold. Then on the next iteration, the model should respond
            # with text only (no more tool_use), ending the turn.
            messages.append({"role": "user", "content": tool_results})
            continue

        # All tools executed; feed results back for the model to continue.
        messages.append({"role": "user", "content": tool_results})

    # If we got here, we hit the loop cap. End gracefully.
    yield {"type": "text", "delta": "\n\nI hit my tool-call limit. Let me stop there.\n"}
    yield {"type": "done", "stop_reason": "max_tool_loops"}


def _describe_external(name: str, args: dict) -> str:
    """Build a short human description of an external action for the
    confirmation prompt + the /api/memory panel."""
    if name == "send_message":
        text = args.get("text", "")
        chat_id = args.get("chat_id")
        target = "Telegram (home)" if not chat_id else f"Telegram chat {chat_id}"
        # Truncate for the spoken prompt
        preview = text if len(text) <= 80 else text[:77] + "..."
        return f"{target}: {preview!r}"
    return f"{name}({args})"


def collect_reply(
    user_text: str,
    history: list[dict] | None = None,
    session_id: str | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = MAX_TOKENS,
) -> tuple[str, float]:
    """Non-streaming version. Returns (full_text, elapsed_seconds)."""
    t0 = time.time()
    text_parts = []
    for evt in stream_reply(user_text, history=history, session_id=session_id,
                            model=model, max_tokens=max_tokens):
        if evt.get("type") == "text":
            text_parts.append(evt.get("delta", ""))
    return "".join(text_parts).strip(), time.time() - t0


def resolve_pending(session_id: str, decision: str) -> dict | None:
    """Resolve a held confirmation: 'yes' executes, 'no' drops. Returns
    the tool result if executed, else None. After resolution the held
    action is cleared."""
    held = _PENDING_CONFIRM.pop(session_id, None)
    if not held:
        return None
    if decision == "yes":
        result = tools.execute(held["tool"], held["args"])
        _log_tool_call(session_id, held["tool"], held["args"], result,
                       tools.tier_of(held["tool"]) or "external_write")
        return result
    return {"cancelled": True, "held": held}


# ---- Session store (delegated to memory.py for disk persistence) ----


def get_history(session_id: str, max_turns: int = 10) -> list[dict]:
    """Return last N turns for a session. Disk-persisted via memory.py."""
    return memory.load_history(session_id, max_turns)


def append_turn(session_id: str, user_text: str, assistant_text: str) -> None:
    """Append a completed turn to the on-disk session log."""
    memory.append_turn(session_id, "user", user_text)
    memory.append_turn(session_id, "assistant", assistant_text)


def clear_session(session_id: str) -> None:
    memory.clear_session(session_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    print("=== testing brain.collect_reply with memory ===")
    text, elapsed = collect_reply("What's my wife's name?")
    print(f"  text={text!r}")
    print(f"  elapsed={elapsed:.2f}s")
