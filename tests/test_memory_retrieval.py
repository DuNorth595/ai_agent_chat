# SPDX-FileCopyrightText: 2026 S_DevLabs
# SPDX-License-Identifier: MIT
"""Regression test for the bridge's memory retrieval.

Justin, 2026-10-26: asked the dev bridge *"what project are we
currently working on?"* and it answered with the MORNING's work
(Sentinel / meds reminder) instead of tonight's voice-bridge work.

Three independent defects, all silent:

1. **The fact store pointed at the wrong database.** The bridge was
   configured for `~/.hermes/memories/fact_store.db` — a 0-byte file
   from July 27 — while the real store (290 facts) lives at
   `~/.hermes/memory_store.db`. Every turn logged
   `fact_store FTS query failed: no such table: facts_fts`, and the
   fact store silently contributed NOTHING to any answer. 189
   occurrences in prod, 31 in dev.

2. **`read_file` could only read the TOP of a file.** The daily log
   was 1562 lines; the bridge read the first 200 (the morning's work)
   and had no way to reach the recent entries at the bottom. There was
   no `tail` and no `offset`.

3. **The "current project" answer was two levels deep and not on the
   cold-start path.** `SAM/PROJECTS_INDEX.md` answers the question
   explicitly ("What was I working on?" → Section A, live/active) but
   the bridge never opened it — it searched for "current project" and
   "ACTIVE" and landed on the wrong files.

The fix: point at the real fact store, give `read_file` a `tail`,
and add a `SAM/CURRENT.md` pointer that is injected into the system
prompt on every turn.

Run:
    env -u PYTHONPATH .venv-iris/bin/python tests/test_memory_retrieval.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def test_fact_store_is_reachable():
    print("\n[test] the fact store actually resolves and returns facts")
    import memory

    check("FACT_STORE_DB exists", memory.FACT_STORE_DB.exists(),
          str(memory.FACT_STORE_DB))
    check("FACT_STORE_DB is not the 0-byte stub",
          memory.FACT_STORE_DB.stat().st_size > 0 if memory.FACT_STORE_DB.exists() else False,
          str(memory.FACT_STORE_DB))

    out = memory._read_fact_store_for_query("voice bridge duplex")
    check("fact query returns content", len(out) > 0, f"{len(out)} chars")
    check("no FTS error", "facts_fts" not in out)


def test_intent_routing_reaches_real_memory():
    print("\n[test] intent routing reaches the REAL memory files")
    import memory

    # The bridge must NOT have a parallel memory store. It reads the
    # same LIFE_MEMORY tree the rest of Sam uses.
    src = Path(memory.__file__).read_text(encoding="utf-8")
    check("no CURRENT.md pointer in the bridge",
          "CURRENT_PATH" not in src and "CURRENT.md" not in src)

    hits = memory._route_life_memory_files(
        "What project are we currently working on?")
    names = [h.name for h in hits]
    check("routes to the real projects index",
          any("PROJECTS_INDEX" in n for n in names), str(names))

    # It must route to files that actually exist in the real tree.
    check("every routed file exists",
          all(h.exists() for h in hits), str(names))

    # "what did we do today" -> the NEWEST daily log, not an old one.
    daily = memory._route_life_memory_files("what did we do today")
    check("today routes to a daily log",
          len(daily) == 1 and daily[0].parent.name == "DAILY",
          str([h.name for h in daily]))

    # "who am i" -> the identity file, not a random family member.
    who = memory._route_life_memory_files("who am i")
    check("who am i routes to justin.md",
          len(who) == 1 and who[0].name == "justin.md",
          str([h.name for h in who]))

    # An unrelated question must NOT route anywhere.
    unrelated = memory._route_life_memory_files("what is the weather")
    check("unrelated query routes nowhere", unrelated == [],
          str([h.name for h in unrelated]))


def test_retrieved_context_is_the_real_index():
    print("\n[test] the retrieved context is the real projects index")
    import memory

    out = memory._read_life_memory_for_query(
        "What project are we currently working on?")
    check("context is non-empty", len(out) > 0, f"{len(out)} chars")
    check("context names the real index file", "PROJECTS_INDEX" in out)
    check("context does NOT name the unrelated Sentinel note",
          "DVMFNE" not in out and "systemctl status" not in out)


def test_read_file_can_read_the_end():
    print("\n[test] read_file can reach the END of a long file")
    import tools

    # Build a file with a marker at the very top and another at the end.
    tmp = Path("/tmp/ava_tail_test.txt")
    tmp.write_text(
        "OLDEST_MARKER_LINE\n" + ("filler\n" * 500) + "NEWEST_MARKER_LINE\n",
        encoding="utf-8",
    )

    head = tools.read_file({"path": str(tmp), "max_lines": 10})
    check("default read sees the top", "OLDEST_MARKER_LINE" in head["content"])
    check("default read does NOT see the end",
          "NEWEST_MARKER_LINE" not in head["content"])
    check("default read reports truncation", head.get("truncated") is True)

    tail = tools.read_file({"path": str(tmp), "tail": 5})
    check("tail sees the end", "NEWEST_MARKER_LINE" in tail["content"])
    check("tail does NOT see the top", "OLDEST_MARKER_LINE" not in tail["content"])
    check("tail reports the line range", tail.get("read_from") == "tail")

    off = tools.read_file({"path": str(tmp), "offset": 500, "max_lines": 10})
    check("offset starts where asked",
          off.get("line_range", "").startswith("500"), str(off.get("line_range")))

    tmp.unlink(missing_ok=True)


def test_read_file_handles_bad_args():
    print("\n[test] read_file handles bad tail/offset values")
    import tools

    tmp = Path("/tmp/ava_tail_bad.txt")
    tmp.write_text("a\nb\nc\n", encoding="utf-8")

    bad = tools.read_file({"path": str(tmp), "tail": "not-a-number"})
    check("non-integer tail is an error", "error" in bad, str(bad))

    bad2 = tools.read_file({"path": str(tmp), "offset": "xyz"})
    check("non-integer offset is an error", "error" in bad2, str(bad2))

    ok = tools.read_file({"path": str(tmp), "tail": 2})
    check("valid tail still works", "b" in ok["content"] and "c" in ok["content"])

    tmp.unlink(missing_ok=True)


def test_daily_log_is_reachable_from_the_end():
    print("\n[test] the real daily log's recent work is reachable")
    import tools

    daily = Path.home() / "Desktop" / "LIFE_MEMORY" / "DAILY" / "2026-10-26.md"
    if not daily.exists():
        print("  SKIP  daily log not present")
        return

    head = tools.read_file({"path": str(daily), "max_lines": 200})
    tail = tools.read_file({"path": str(daily), "tail": 60})
    check("head read is truncated", head.get("truncated") is True)
    check("tail read reaches the recent entries",
          "Prod" in tail["content"] or "prod" in tail["content"],
          "tail should contain tonight's work")
    check("head and tail differ", head["content"] != tail["content"])


def main():
    print("=" * 60)
    print("memory retrieval regression test")
    print("=" * 60)
    test_fact_store_is_reachable()
    test_intent_routing_reaches_real_memory()
    test_retrieved_context_is_the_real_index()
    test_read_file_can_read_the_end()
    test_read_file_handles_bad_args()
    test_daily_log_is_reachable_from_the_end()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
