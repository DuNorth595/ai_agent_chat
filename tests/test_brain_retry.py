# SPDX-FileCopyrightText: 2026 S_DevLabs
# SPDX-License-Identifier: MIT
"""Regression test for the voice bridge's brain retry path.

Justin, 2026-10-26: driving home, asked the bridge about traffic, and
got "I hit a brain error." twice.

Root cause: MiniMax returned HTTP 529 ("server cluster under high
load") on two consecutive turns. `brain.py` had NO retry — one 529 and
the turn was over. The user lost the whole answer to a momentary
service-side blip.

Hermes' own agent already handles this: it classifies 503/529 as
overloaded and backs off with `agent.api_max_retries`
(`hermes-agent/agent/error_classifier.py:1305`). The bridge had no
equivalent.

This test drives `brain.stream_reply` with a stubbed HTTP layer that
fails a configurable number of times, then succeeds. It asserts:

1. A transient failure is retried and the answer still arrives.
2. A NON-transient failure (401) is NOT retried.
3. The retry uses backoff, not a tight loop.
4. The spoken error text is specific, not the old generic string.

Run:
    .venv-iris/bin/python tests/test_brain_retry.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


class FakeResponse:
    """Minimal stand-in for a requests streaming response."""

    def __init__(self, status_code=200, lines=None):
        self.status_code = status_code
        self._lines = lines or []

    def iter_lines(self, decode_unicode=True):
        for line in self._lines:
            yield line

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def install_stub(monkey, failures, fail_status=529, success_lines="__DEFAULT__"):
    """Replace requests.post in the brain module with a stub.

    `failures` = how many times to fail before succeeding.
    """
    import brain

    if success_lines == "__DEFAULT__":
        success_lines = success_stream()

    calls = {"n": 0}
    sleeps = []

    def fake_post(url, **kwargs):
        calls["n"] += 1
        if calls["n"] <= failures:
            resp = FakeResponse(status_code=fail_status)
            # brain.py reads response.status_code, and the real
            # requests.Response also carries raise_for_status. Provide
            # both so the failure path behaves like production.
            resp.raise_for_status = lambda: (_ for _ in ()).throw(
                brain.requests.exceptions.HTTPError(
                    f"{fail_status} Server Error", response=resp
                )
            )
            return resp
        resp = FakeResponse(status_code=200, lines=success_lines)
        resp.raise_for_status = lambda: None
        return resp

    real_post = brain.requests.post
    real_sleep = brain.time.sleep
    brain.requests.post = fake_post
    brain.time.sleep = lambda s: sleeps.append(s)
    return calls, sleeps, (real_post, real_sleep), real_sleep


def restore(brain, saved):
    real_post, real_sleep = saved
    brain.requests.post = real_post
    brain.time.sleep = real_sleep


def success_stream(text="Traffic is clear."):
    """Build a valid Anthropic SSE stream yielding one text block."""
    import json
    return [
        "data: " + json.dumps({
            "type": "message_start",
            "message": {"usage": {"input_tokens": 10}},
        }),
        "data: " + json.dumps({
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        }),
        "data: " + json.dumps({
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
        }),
        "data: " + json.dumps({"type": "message_stop"}),
        "data: [DONE]",
    ]


def collect(gen):
    """Drain a brain.stream_reply generator into text + stop_reason.

    brain.stream_reply yields DICTS with a "type" key, not
    (kind, payload) tuples -- the bridge's consumer unwraps them.
    """
    out = []
    stop = None
    for payload in gen:
        kind = payload.get("type")
        if kind == "text":
            out.append(payload.get("delta", ""))
        elif kind == "done":
            stop = payload.get("stop_reason")
    return "".join(out), stop


def test_transient_is_retried():
    print("\n[test] a transient 529 is retried and the answer still arrives")
    import brain
    calls, sleeps, saved, _ = install_stub(brain, failures=2)
    try:
        t0 = time.time()
        text, stop = collect(brain.stream_reply("test", session_id="retry-1"))
        print(f"      POST calls: {calls['n']}  sleeps: {sleeps}")
        check("retried the failing calls", calls["n"] == 3, f"calls={calls['n']}")
        check("answer arrived", "Traffic is clear" in text, repr(text))
        check("not an error stop", stop == "end_turn", f"stop={stop}")
        check("backoff was used", len(sleeps) >= 2, f"sleeps={sleeps}")
        check("backoff increases", len(sleeps) < 2 or sleeps[1] > sleeps[0],
              f"sleeps={sleeps}")
    finally:
        restore(brain, saved)


def test_recovers_on_second_attempt():
    print("\n[test] recovers when only the first call fails")
    import brain
    calls, sleeps, saved, _ = install_stub(brain, failures=1)
    try:
        text, stop = collect(brain.stream_reply("test", session_id="retry-2"))
        check("two POST calls", calls["n"] == 2, f"calls={calls['n']}")
        check("answer arrived", "Traffic is clear" in text, repr(text))
    finally:
        restore(brain, saved)


def test_non_transient_is_not_retried():
    print("\n[test] a 401 is NOT retried")
    import brain
    calls, sleeps, saved, _ = install_stub(brain, failures=99, fail_status=401)
    try:
        text, stop = collect(brain.stream_reply("test", session_id="retry-3"))
        print(f"      POST calls: {calls['n']}")
        check("only one POST call", calls["n"] == 1, f"calls={calls['n']}")
        check("no backoff sleeps", len(sleeps) == 0, f"sleeps={sleeps}")
        check("stop_reason is error", stop == "error", f"stop={stop}")
        check("mentions credentials", "credential" in text.lower(), repr(text))
    finally:
        restore(brain, saved)


def test_persistent_transient_eventually_gives_up():
    print("\n[test] persistent 529 gives up after the retry budget")
    import brain
    calls, sleeps, saved, _ = install_stub(brain, failures=99, fail_status=529)
    try:
        text, stop = collect(brain.stream_reply("test", session_id="retry-4"))
        expected = brain.BRAIN_MAX_RETRIES + 1
        print(f"      POST calls: {calls['n']} (budget {brain.BRAIN_MAX_RETRIES})")
        check("stopped at the budget", calls["n"] == expected,
              f"calls={calls['n']} expected={expected}")
        check("stop_reason is error", stop == "error", f"stop={stop}")
        check("says overloaded", "overloaded" in text.lower(), repr(text))
    finally:
        restore(brain, saved)


def test_error_text_is_short_and_specific():
    print("\n[test] the spoken error text is short and specific")
    import brain
    check("529 -> overloaded wording",
          "overloaded" in brain._friendly_provider_error(529).lower())
    check("429 -> rate limit wording",
          "rate limited" in brain._friendly_provider_error(429).lower())
    check("401 -> credentials wording",
          "credential" in brain._friendly_provider_error(401).lower())
    check("500 -> server error wording",
          "server error" in brain._friendly_provider_error(500).lower())
    for code in (429, 500, 503, 529, 401, 403, None):
        msg = brain._friendly_provider_error(code)
        # Long error strings in session history poison future turns --
        # the model imitates them verbatim. Keep them short.
        check(f"short message for {code}", len(msg) < 120, f"{len(msg)} chars")


def test_transient_status_set():
    print("\n[test] the retryable status set")
    import brain
    for code in (429, 500, 502, 503, 504, 529):
        check(f"{code} is transient", brain._is_transient_status(code))
    for code in (400, 401, 403, 404, 422, None):
        check(f"{code} is not transient", not brain._is_transient_status(code))


def main():
    print("=" * 60)
    print("brain retry regression test")
    print("=" * 60)
    test_transient_status_set()
    test_error_text_is_short_and_specific()
    test_transient_is_retried()
    test_recovers_on_second_attempt()
    test_non_transient_is_not_retried()
    test_persistent_transient_eventually_gives_up()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
