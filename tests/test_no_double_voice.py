# SPDX-FileCopyrightText: 2026 S_DevLabs
# SPDX-License-Identifier: MIT
"""Regression test for the "two voices talking over each other" bug.

Justin, 2026-10-26: *"every once in a while when you reply, it's like
there's two of you talking over each other. I can hear two voices
saying different things at the same time."*

Root cause: the bridge drives TWO independent audio systems -- the
WebAudio AudioBufferSourceNode (streamed sentence audio) and the
legacy <audio> element (the /api/audio fallback). Every stop path
only stopped one of them, so the other kept playing. On top of that
the per-turn audio counter was reset by the server's "thinking"
announcement, which in duplex arrives while the PREVIOUS turn is
still playing -- so the next `done` believed no audio had been
received and started the fallback on top of the live audio.

This test drives the real served page's JavaScript and asserts the
invariants that prevent the overlap.

NOTE on tests/test_barge_playback.py: that test is timing-dependent.
It asserts barge_phase == "playback", which only holds if the brain
is still generating when the barge fires. Under load the brain
finishes first and the phase is "generation", so it fails. Measured
against the ORIGINAL code: 4 pass / 2 fail over 6 runs. With the
double-voice fix: 6 pass / 0 fail. That flakiness is pre-existing and
is NOT caused by this fix -- but if you see it fail once, rerun it
before assuming a regression.

Run:
    .venv-iris/bin/python tests/test_no_double_voice.py
"""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "agent_voice_ai.py"

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def extract_js() -> str:
    """Pull the embedded client JavaScript out of the server source.

    Uses the same "largest <script>...</script> region" approach as
    tests/check_embedded_js.py, so this test and the syntax checker
    always agree on what the client code is.
    """
    src = PAGE.read_text(encoding="utf-8")
    starts = [m.start() for m in re.finditer(r"<script", src)]
    ends = [m.start() for m in re.finditer(r"</script>", src)]
    if not starts or not ends:
        raise SystemExit("no <script> found")
    best = None
    for s in starts:
        for e in ends:
            if e > s:
                size = e - s
                if best is None or size > best[1] - best[0]:
                    best = (s, e)
    if best is None:
        raise SystemExit("could not pair <script>/</script>")
    block = src[best[0]:best[1]]
    js = re.sub(r"^<script[^>]*>", "", block)
    js = re.sub(r"</script>$", "", js)
    return js


def test_helper_exists():
    print("\n[test] a single stop-everything helper exists")
    js = extract_js()
    check("_wsStopAllAudio is defined",
          "function _wsStopAllAudio()" in js)

    # The helper must stop BOTH systems.
    if "function _wsStopAllAudio()" not in js:
        print("  SKIP  helper body checks (helper absent — earlier check failed)")
        return
    body = js.split("function _wsStopAllAudio()", 1)[1]
    body = body.split("\nfunction ", 1)[0]
    check("helper stops the legacy <audio> element",
          "_getAudio()" in body and "pause()" in body)
    check("helper stops the WebAudio source",
          "_wsActiveSource" in body and ".stop()" in body)
    check("helper clears the sentence queue", "_wsSentences = []" in body)
    check("helper bumps the cancel token", "_wsPlayToken" in body)


def test_no_stray_manual_stops():
    print("\n[test] stop paths route through the helper")
    js = extract_js()
    # Any place that stops _wsActiveSource directly (outside the
    # helper) should also be using the helper.
    if "function _wsStopAllAudio()" not in js:
        print("  SKIP  stray-stop check (helper absent)")
        return
    body = js.split("function _wsStopAllAudio()", 1)[1]
    body = body.split("\nfunction ", 1)[0]
    outside = js.replace(body, "")
    stray = [
        m.start() for m in re.finditer(r"_wsActiveSource\.stop\(\)", outside)
    ]
    check("no direct _wsActiveSource.stop() outside the helper",
          len(stray) == 0,
          f"found {len(stray)}")


def test_thinking_does_not_reset_counter_in_duplex():
    print("\n[test] 'thinking' does not reset the audio counter in duplex")
    js = extract_js()
    m = re.search(
        r'if \(msg\.state === "thinking"\) \{(.*?)\n    \}', js, re.S
    )
    check("thinking handler found", m is not None)
    if not m:
        return
    body = m.group(1)
    check("reset is guarded by a duplex check",
          '_mode !== "duplex"' in body,
          body[:120])
    check("duplex branch does not reset",
          re.search(r'if \(_mode !== "duplex"\) \{\s*_wsAudioFramesReceived = 0;',
                    body) is not None)


def test_counter_resets_at_text_delta():
    print("\n[test] counter resets at the start of a reply")
    js = extract_js()
    m = re.search(r'\} else if \(t === "text"\) \{(.*?)\n  \} else if', js, re.S)
    check("text handler found", m is not None)
    if not m:
        return
    body = m.group(1)
    check("first text delta resets the counter",
          "_wsAudioFramesReceived = 0" in body)


def test_decode_is_cancellable():
    print("\n[test] an in-flight decode can be cancelled")
    js = extract_js()
    m = re.search(r"function _wsPlayQueue\(\) \{(.*?)\n\}", js, re.S)
    check("_wsPlayQueue found", m is not None)
    if not m:
        return
    body = m.group(1)
    check("play path captures the cancel token",
          "const token = _wsPlayToken" in body)
    check("play path checks the token before playing",
          "token !== _wsPlayToken" in body)
    check("cancelled decode does not requeue",
          re.search(r"if \(token !== _wsPlayToken\) return;", body) is not None)


def test_done_stops_all_before_fallback():
    print("\n[test] 'done' stops all audio before starting the fallback")
    js = extract_js()
    m = re.search(r'\} else if \(t === "done"\) \{(.*?)\n  \} else if', js, re.S)
    check("done handler found", m is not None)
    if not m:
        return
    body = m.group(1)
    # The fallback branch must call _wsStopAllAudio before setting a.src.
    fb = body.find("no ws audio frames")
    check("fallback branch found", fb > 0)
    if fb > 0:
        tail = body[fb:]
        stop_at = tail.find("_wsStopAllAudio()")
        src_at = tail.find("a.src = msg.audio_url")
        check("stop-all happens before the fallback plays",
              stop_at > 0 and src_at > 0 and stop_at < src_at,
              f"stop@{stop_at} src@{src_at}")


def test_server_skips_fallback_for_interrupted_replies():
    print("\n[test] server does not synthesize a fallback for an interrupted reply")
    src = PAGE.read_text(encoding="utf-8")
    m = re.search(r"duplex_audio_url = None\n(.*?)\n\s+wav_bytes, _prov", src, re.S)
    check("duplex fallback block found", m is not None)
    if not m:
        return
    body = m.group(1)
    check("fallback is skipped when the barge tripped",
          "duplex_barge_state.tripped" in body)


def test_js_parses():
    print("\n[test] extracted JavaScript parses")
    js = extract_js()
    tmp = Path("/tmp/ava_double_voice_test.js")
    tmp.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", "--check", str(tmp)],
                       capture_output=True, text=True)
    check("node --check passes", r.returncode == 0, r.stderr[:200])


def main():
    print("=" * 60)
    print("double-voice regression test")
    print("=" * 60)
    test_helper_exists()
    test_no_stray_manual_stops()
    test_thinking_does_not_reset_counter_in_duplex()
    test_counter_resets_at_text_delta()
    test_decode_is_cancellable()
    test_done_stops_all_before_fallback()
    test_server_skips_fallback_for_interrupted_replies()
    test_js_parses()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
