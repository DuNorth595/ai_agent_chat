#!/usr/bin/env python3
"""Test that the text input box stays reachable (v0.10.8).

Justin: "after a few back and forth conversations, the text box where
I can type into disappears."

This was a CSS layout bug, and a subtle one. The chain:

  - `html` is `height:100%; overflow:hidden` -- the PAGE cannot scroll,
    so anything below the fold is simply gone.
  - `body` was a flex COLUMN. `.stage` (the orb) was `flex:1` with the
    default `min-height:auto`, so it refused to shrink below its 200px
    orb and pushed the conversation area down.
  - `.chat-shell` then sized to its CONTENT, and `.transcript` had no
    height cap, so the shell grew to the full length of the
    conversation.
  - The `.controls` row -- the row holding the text input -- ended up at
    y=7870 in a 633px viewport. Gone, with no way to scroll to it.

The fix is a grid with explicit rows, `min-height: 0` on the grid
items, and the transcript as the only scrolling region.

This test asserts the RULES that produce the behaviour, because the
layout itself can only be measured in a real browser. The browser
check that goes with it lives in the same file's sibling notes: serve
the page over plain HTTP, inject turns, and assert the input's
getBoundingClientRect() stays inside the viewport.

Run:  .venv-iris/bin/python tests/test_input_box_visible.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}")
        print(f"  FAIL  {name} {detail}")


def get_source():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "agent_voice_ai.py"), encoding="utf-8") as f:
        return f.read()


def css_block(src, selector, at_line_start=False):
    """Return the CSS text for a rule, or '' if absent.

    The CSS lives inside a Python string, so the selectors in the
    source are indented with 2 or 4 spaces depending on nesting.

    `at_line_start=True` anchors the match to the beginning of a line
    (after optional whitespace). That is how you tell the `body` rule
    apart from the shared `html, body` reset -- without the anchor a
    search for "body" happily matches "html, body" and the test
    asserts against the wrong rule.
    """
    esc = re.escape(selector)
    if at_line_start:
        pattern = r"^[ 	]*" + esc + r"\s*\{([^}]*)\}"
    else:
        pattern = esc + r"\s*\{([^}]*)\}"
    m = re.search(pattern, src, re.M)
    return m.group(1) if m else ""


def test_layout_rules():
    print("\n── layout rules ──")
    src = get_source()

    # The page must not scroll -- so nothing may overflow it.
    check("html is still overflow:hidden (the constraint that hid the box)",
          re.search(r"html,\s*body\s*\{[^}]*overflow:\s*hidden", src) is not None)

    # The body-only rule, not the shared "html, body" reset. The
    # shared rule is written as "html, body {" on one line, so a
    # match on a line that starts with "body {" finds the dedicated
    # rule. Match on the LINE START so "html, body {" cannot match.
    body = css_block(src, "body", at_line_start=True)
    check("body is a grid with explicit rows",
          "display: grid" in body and "grid-template-rows" in body,
          body[:120])
    check("body grid has three rows (header / stage / chat)",
          body.count("minmax") >= 1 and "auto" in body, body[:120])

    stage = css_block(src, ".stage")
    check("stage can shrink (min-height: 0)",
          "min-height: 0" in stage, stage[:140])
    check("stage does not overflow its row",
          "overflow: hidden" in stage, stage[:140])

    shell = css_block(src, ".chat-shell")
    check("chat-shell can shrink (min-height: 0)",
          "min-height: 0" in shell, shell[:140])

    transcript = css_block(src, ".transcript")
    check("transcript is the scrolling region (overflow-y: auto)",
          "overflow-y: auto" in transcript, transcript[:140])
    check("transcript can shrink (min-height: 0)",
          "min-height: 0" in transcript, transcript[:140])
    check("transcript has a height cap",
          "max-height" in transcript, transcript[:140])

    controls = css_block(src, ".controls")
    # v0.10.8 — the input row FLOATS: it is absolutely positioned at
    # the bottom of the app and lifted by the keyboard height, so it
    # can sit above the keyboard. A flow item cannot do that -- the
    # keyboard overlays the app, so a margin would push the row off
    # the screen instead of lifting it.
    check("the input row floats (position: absolute)",
          "position: absolute" in controls, controls[:140])
    check("the input row is anchored to the bottom",
          "bottom: 0" in controls, controls[:140])
    check("the input row is lifted by the keyboard height",
          "keyboard-height" in controls, controls[:140])
    check("controls row is pinned (flex: 0 0 auto)",
          "flex: 0 0 auto" in controls, controls[:140])

    # The input is the only item that may shrink, and it must be
    # allowed to shrink BELOW its intrinsic size. A flex item's
    # default min-width:auto floors it at the text input's `size`
    # attribute (~200px), which is what pushed the row to 356px on a
    # 320px iPhone SE and put the clear button out of reach.
    ctl_input = css_block(src, '.controls input[type="text"]')
    check("input can shrink below its intrinsic floor (min-width: 0)",
          "min-width: 0" in ctl_input, ctl_input[:140])
    check("input is the flexible item (flex: 1 1 0)",
          "flex: 1 1 0" in ctl_input, ctl_input[:140])
    check("controls buttons do not shrink",
          "flex: 0 0 auto" in controls, controls[:140])

    orb = css_block(src, ".orb")
    check("orb scales instead of forcing a fixed height",
          "min(" in orb and "100%" in orb, orb[:140])


def test_removed_controls():
    print("\n── controls that were moved out of the input row ──")
    src = get_source()
    check("audio test button is gone",
          "audio-test-btn" not in src and "audioTestBtn" not in src)
    check("debug button lives in the header",
          'id="debug-btn"' in src.split('class="header"')[1][:2000]
          if 'class="header"' in src else False)
    check("no separate dev-tools row",
          "dev-tools" not in src)
    # v0.10.8 — the Pause button was built and then removed at Justin's
    # direction ("The pause button never worked; I gave up on it").
    # It must not creep back: a pause that aborts the brain is a stop,
    # and a stop that discards the rest of the reply cannot be resumed.
    check("no pause button",
          "pause-btn" not in src and "_wsPaused" not in src)
    check("no pause/resume socket messages",
          'kind == "pause"' not in src and 'kind == "resume"' not in src)
    check("the input row holds only conversation controls",
          src.count('id="text-input"') == 1)
    check("the input row holds only conversation controls",
          src.count('id="text-input"') == 1)


def test_no_regression_guards():
    print("\n── the trap that keeps biting ──")
    src = get_source()

    body = css_block(src, "body", at_line_start=True)

    # v0.10.8 — the app must NEVER be sized to the visual viewport.
    # That was the cause of the "shakes up and down when I click
    # send": the body was sized to visualViewport.height, so the
    # keyboard opening shrank the app and closing it grew the app
    # back, re-laying-out the grid each time. The screen recording
    # showed the orb's bright peak moving 197 -> 277 -> 286 -> 319
    # -> 418 as the keyboard closed.
    #
    # The app is always the full screen. The keyboard overlays it,
    # and the input row is the only thing that moves.
    check("the app is NOT sized to the visual viewport",
          "--app-height" not in body,
          "the body uses --app-height, so it resizes with the keyboard")
    check("the app is always the full screen",
          "height: 100%" in body, body[:140])

    # The GRID ROW is the guard, not the transcript's flex value.
    #
    # The transcript used to be `flex: 0 1 auto` with no height cap,
    # and the shell row was `minmax(0, auto)` -- CONTENT-SIZED. Adding
    # a turn grew the shell, the 1fr stage row shrank, and the orb
    # (anchored to the stage's top) jumped. That is the "shakes up and
    # down after I hit send" bug.
    #
    # Now the shell row is a FIXED share of the height, and the
    # transcript fills it and scrolls inside. So the transcript SHOULD
    # be `flex: 1 1 auto` -- what matters is that the row above it
    # cannot grow.
    body = css_block(src, "body", at_line_start=True)
    check("the shell row has a FIXED height share (not content-sized)",
          "45vh" in body or "vh" in body.split("grid-template-rows")[-1][:120],
          body.split("grid-template-rows")[-1][:120])
    # Only the THIRD row matters. The first row is the header, which
    # is correctly `auto` -- it is as tall as it needs to be.
    rows = body.split("grid-template-rows")[-1].split(";")[0]
    third = rows.split()[-1] if rows.split() else ""
    check("the shell row is NOT content-sized (that is the shake)",
          "auto" not in third, f"third row is {third!r}")

    transcript = css_block(src, ".transcript")
    check("the transcript fills the fixed shell (flex: 1 1 auto)",
          "flex: 1 1 auto" in transcript, transcript[:140])
    check("the transcript can shrink (min-height: 0)",
          "min-height: 0" in transcript, transcript[:140])
    check("the transcript scrolls internally",
          "overflow-y: auto" in transcript, transcript[:140])


if __name__ == "__main__":
    print("=" * 62)
    print("v0.10.8 — the text input box stays reachable")
    print("=" * 62)

    test_layout_rules()
    test_removed_controls()
    test_no_regression_guards()

    print("\n" + "=" * 62)
    print(f"passed: {PASS}   failed: {FAIL}")
    for f in FAILURES:
        print(f"  - {f}")
    print("RESULT:", "PASS" if FAIL == 0 else "FAIL")
    print("=" * 62)
    sys.exit(0 if FAIL == 0 else 1)
