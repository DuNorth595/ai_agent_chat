#!/usr/bin/env python3
"""Measure the ai_agent_chat controls row at real iPhone viewports.

Chrome's --window-size sets the WINDOW, not the layout viewport, so
media queries still see a wide viewport. The only reliable way to test
a real iPhone width is CDP's Emulation.setDeviceMetricsOverride, which
sets the layout viewport exactly as the device would.

This measures what matters:
  - does the input row fit the viewport width?
  - is every control reachable (right edge <= viewport)?
  - is the text input still on screen vertically?
  - is the debug button in the header's top right?

Run:  .venv-iris/bin/python /path/to/measure_iphone.py
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
# The page under test. Served over plain HTTP on a scratch port so
# headless Chrome can load it without a certificate error -- the real
# bridge is HTTPS with a self-signed cert, which Chrome refuses to
# load headlessly.
#
# AVA_PORT is the bridge under test (dev is 8771, prod is 8770).
# AVA_HTTP_PORT is the scratch port for the plain-HTTP copy.
AVA_PORT = os.environ.get("AVA_PORT", "8771")
HTTP_PORT = int(os.environ.get("AVA_HTTP_PORT", "8899"))
PAGE = f"http://127.0.0.1:{HTTP_PORT}/ava_page.html"

# Where the plain-HTTP copy of the page is written. Kept out of the
# repo -- this is a scratch artifact.
SCRATCH_DIR = os.path.join(
    os.environ.get("TMPDIR", "/tmp").rstrip("/"), "ava_layout_test"
)
PORT = 9222

WIDTHS = [
    (320, 568, "iPhone SE (1st gen)"),
    (375, 667, "iPhone SE 2/3"),
    (390, 844, "iPhone 12/13/14"),
    (393, 852, "iPhone 14 Pro"),
    (414, 896, "iPhone XR/11"),
    (430, 932, "iPhone 15 Pro Max"),
]

MEASURE_JS = r"""
(() => {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const ctrl = document.querySelector('.controls');
  const cb = ctrl.getBoundingClientRect();
  const items = [...ctrl.children].map(el => {
    const b = el.getBoundingClientRect();
    return {id: el.id || el.tagName, w: Math.round(b.width),
            right: Math.round(b.right), text: (el.textContent||'').trim().slice(0,10)};
  });
  const inp = document.getElementById('text-input');
  const ib = inp ? inp.getBoundingClientRect() : null;
  const dbg = document.getElementById('debug-btn');
  const dbb = dbg ? dbg.getBoundingClientRect() : null;
  const hdr = document.querySelector('.header');
  const hb = hdr ? hdr.getBoundingClientRect() : null;

  // The orb, in BOTH states. "thinking" is the state duplex uses, and
  // it is the one that rotates -- a rotating ellipse reads as a "weird
  // spinning shape", which is exactly what Justin saw.
  const orb = document.querySelector('.orb');
  let orbIdle = null, orbThinking = null, orbRadius = null, orbAnim = null;
  if (orb) {
    const b0 = orb.getBoundingClientRect();
    orbIdle = {w: Math.round(b0.width), h: Math.round(b0.height)};
    const cs = getComputedStyle(orb);
    orbRadius = cs.borderRadius;
    orbAnim = cs.animationName;
    // Measure in the "thinking" state -- that is the state duplex uses,
    // and the one that rotates. A rotating ellipse is what looked like
    // a "weird spinning shape", so the shape must be checked while the
    // spin is applied.
    orb.classList.add('thinking');
    const b1 = orb.getBoundingClientRect();
    orbThinking = {w: Math.round(b1.width), h: Math.round(b1.height)};
    const cs2 = getComputedStyle(orb);
    orbAnim = cs2.animationName;
    orb.classList.remove('thinking');
  }
  const isSquare = (o) => !!o && Math.abs(o.w - o.h) <= 1;

  return JSON.stringify({
    vw: vw, vh: vh,
    controls_w: Math.round(cb.width),
    items: items,
    row_fits: items.every(it => it.right <= vw + 1),
    input_w: ib ? Math.round(ib.width) : 0,
    input_onscreen: ib ? (ib.bottom <= vh + 1 && ib.top >= -1) : false,
    input_bottom: ib ? Math.round(ib.bottom) : -1,
    dbg_in_header: !!(dbg && dbg.closest('.header')),
    dbg_right: dbb ? Math.round(dbb.right) : null,
    dbg_top: dbb ? Math.round(dbb.top) : null,
    dbg_fits: dbb ? dbb.right <= vw + 1 : null,
    header_h: hb ? Math.round(hb.height) : 0,
    orb_idle: orbIdle,
    orb_thinking: orbThinking,
    orb_border_radius: orbRadius,
    orb_animation: orbAnim,
    orb_is_circle: isSquare(orbIdle) && isSquare(orbThinking),
  });
})()
"""


def start_chrome():
    subprocess.run(
        ["pkill", "-f", "remote-debugging-port=9222"],
        capture_output=True,
    )
    subprocess.Popen(
        [
            CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", "--remote-debugging-port=9222",
            "--user-data-dir=/tmp/chrprof_measure",
            "--window-size=430,932", "about:blank",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/json/version", timeout=2
            ) as r:
                return json.loads(r.read().decode())
        except Exception:
            continue
    raise RuntimeError("chrome did not expose the debug port")


_CDP_ID = [0]


async def cdp(ws, method, params=None, session_id=None):
    """Minimal CDP client over the websocket.

    Must be awaited from INSIDE the running loop -- an earlier version
    wrapped this in run_until_complete, which raises "This event loop is
    already running" when called from inside asyncio.run().
    """
    _CDP_ID[0] += 1
    mid = _CDP_ID[0]
    payload = {"id": mid, "method": method, "params": params or {}}
    if session_id:
        payload["sessionId"] = session_id
    await ws.send(json.dumps(payload))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=20)
        msg = json.loads(raw)
        if msg.get("id") == mid:
            if "error" in msg:
                raise RuntimeError(f"{method}: {msg['error']}")
            return msg.get("result", {})


def fetch_page():
    """Pull the live page from the bridge and drop it where the
    scratch HTTP server can serve it."""
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        f"https://127.0.0.1:{AVA_PORT}/",
        headers={"User-Agent": "layout-test"},
    )
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        html = r.read()
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    path = os.path.join(SCRATCH_DIR, "ava_page.html")
    with open(path, "wb") as f:
        f.write(html)
    return path


def start_http_server():
    """Serve the fetched page over plain HTTP for headless Chrome."""
    import functools
    import http.server
    import socketserver

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=SCRATCH_DIR
    )

    class Quiet(socketserver.TCPServer):
        allow_reuse_address = True

        def handle_error(self, request, client_address):
            pass

    httpd = Quiet(("127.0.0.1", HTTP_PORT), handler)
    # Silence the request log; it is pure noise in test output.
    handler.log_message = lambda *a, **k: None
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def main():
    import asyncio
    import websockets

    fetch_page()
    httpd = start_http_server()
    ver = start_chrome()
    ws_url = ver["webSocketDebuggerUrl"]
    results = []
    failures = 0

    async def run():
        nonlocal failures
        async with websockets.connect(
            ws_url, max_size=None, ping_interval=None
        ) as ws:
            # Open a page
            r = await cdp(ws, "Target.createTarget",
                         {"url": PAGE})
            tid = r["targetId"]
            r = await cdp(ws, "Target.attachToTarget",
                         {"targetId": tid, "flatten": True})
            sid = r["sessionId"]

            async def sess(method, params=None):
                return await cdp(ws, method, params, session_id=sid)

            await sess("Page.enable")
            await sess("Runtime.enable")

            # ── the keyboard test ──
            # On iOS the on-screen keyboard shrinks the VISUAL viewport
            # and Safari scrolls the document to reveal the focused
            # input. That scroll moved the PTT/Duplex toggle off the
            # top of the screen. The toggle must stay put when the
            # viewport shrinks.
            print("\n  keyboard simulation (iPhone 14 Pro, 393x852):")
            await sess("Emulation.setDeviceMetricsOverride", {
                "width": 393, "height": 852,
                "deviceScaleFactor": 2, "mobile": True,
            })
            await sess("Page.reload", {"ignoreCache": True})
            await asyncio.sleep(1.2)

            before = await sess("Runtime.evaluate", {
                "expression": """
                (() => {
                  const t = document.getElementById('mode-toggle');
                  const b = t.getBoundingClientRect();
                  return JSON.stringify({
                    top: Math.round(b.top),
                    visible: b.top >= -1 && b.bottom <= window.innerHeight + 1,
                    scrollY: window.scrollY,
                    docScrollTop: document.documentElement.scrollTop,
                    bodyScrollTop: document.body.scrollTop,
                    vh: window.innerHeight,
                  });
                })()
                """,
                "returnByValue": True,
            })
            b = json.loads(before.get("result", {}).get("value") or "{}")

            # Simulate the keyboard the way it actually works: the
            # LAYOUT viewport gets shorter. Overriding
            # window.innerHeight does NOT do this -- it only changes
            # what a script reads, not how the page lays out, which is
            # why the first version of this test passed with and
            # without the fix.
            await sess("Emulation.setDeviceMetricsOverride", {
                "width": 393, "height": 852 - 336,
                "deviceScaleFactor": 2, "mobile": True,
            })
            await asyncio.sleep(0.6)

            after = await sess("Runtime.evaluate", {
                "expression": """
                (() => {
                  const t = document.getElementById('mode-toggle');
                  const b = t.getBoundingClientRect();
                  const st = getComputedStyle(document.body);
                  return JSON.stringify({
                    top: Math.round(b.top),
                    visible: b.top >= -1 && b.bottom <= window.innerHeight + 1,
                    scrollY: window.scrollY,
                    bodyPosition: st.position,
                    bodyHeight: st.height,
                    vh: window.innerHeight,
                  });
                })()
                """,
                "returnByValue": True,
            })
            a = json.loads(after.get("result", {}).get("value") or "{}")

            # The toggle must be ON SCREEN in the shrunken viewport,
            # and it must not have MOVED relative to the top of the
            # app. A scroll that pushed it above y=0 is the bug.
            kb_ok = (a.get("visible") is True
                     and a.get("top", -999) >= 0
                     and a.get("scrollY", 1) == 0
                     and a.get("bodyPosition") == "fixed"
                     and abs(a.get("top", 0) - b.get("top", 0)) <= 2)
            if not kb_ok:
                failures += 1
            print(f"        before: top={b.get('top')} visible={b.get('visible')} "
                  f"scrollY={b.get('scrollY')} vh={b.get('vh')}")
            print(f"        after:  top={a.get('top')} visible={a.get('visible')} "
                  f"scrollY={a.get('scrollY')} body={a.get('bodyPosition')} "
                  f"h={a.get('bodyHeight')} vh={a.get('vh')}")
            print(f"        keyboard keeps the mode toggle: "
                  f"{'OK ' if kb_ok else 'BAD'}")

            for w, h, label in WIDTHS:
                await sess("Emulation.setDeviceMetricsOverride", {
                    "width": w, "height": h,
                    "deviceScaleFactor": 2, "mobile": True,
                })
                await sess("Page.reload", {"ignoreCache": True})
                await asyncio.sleep(1.2)

                # Grow the conversation so the transcript is long --
                # the input box disappeared only after a few back and
                # forth turns, and this is the condition that exposed it.
                await sess("Runtime.evaluate", {
                    "expression": """
                    (() => {
                      const tr = document.getElementById('transcript');
                      const empty = tr.querySelector('.empty');
                      if (empty) empty.remove();
                      for (let i = 0; i < 40; i++) {
                        const d = document.createElement('div');
                        d.className = 'turn assistant';
                        d.innerHTML = '<div class="role">sam</div><div class="text">'
                          + 'This is a long assistant reply used to force the transcript to grow. '.repeat(3)
                          + '</div>';
                        tr.appendChild(d);
                      }
                      return tr.children.length;
                    })()
                    """,
                    "returnByValue": True,
                })
                await asyncio.sleep(0.4)

                # Prove the thinking state actually ROTATES. A
                # non-rotating orb is a different regression from a
                # distorted one, and a "shape that spins" complaint is
                # only fully answered by seeing the rotation.
                spin = await sess("Runtime.evaluate", {
                    "expression": """
                    (async () => {
                      const orb = document.querySelector('.orb');
                      if (!orb) return JSON.stringify({ok: false});
                      orb.classList.add('thinking');
                      const seen = new Set();
                      for (let i = 0; i < 8; i++) {
                        seen.add(getComputedStyle(orb).transform);
                        await new Promise(r => setTimeout(r, 120));
                      }
                      const anim = getComputedStyle(orb).animationName;
                      orb.classList.remove('thinking');
                      return JSON.stringify({
                        ok: true, distinct: seen.size, anim: anim
                      });
                    })()
                    """,
                    "awaitPromise": True, "returnByValue": True,
                })
                spin_data = json.loads(
                    spin.get("result", {}).get("value") or "{}"
                )

                r = await sess("Runtime.evaluate", {
                    "expression": MEASURE_JS, "returnByValue": True,
                })
                val = r.get("result", {}).get("value")
                if not val:
                    print(f"  {w}px -> NO RESULT")
                    failures += 1
                    continue
                d = json.loads(val)
                results.append((w, label, d))

                spinning = bool(spin_data.get("ok")) and \
                    spin_data.get("distinct", 0) > 2
                ok = (d["row_fits"] and d["input_onscreen"]
                      and d["dbg_in_header"] and d["dbg_fits"]
                      and d["orb_is_circle"] and spinning)
                if not ok:
                    failures += 1
                print(f"  {w:>4}px {label:<22} {'OK ' if ok else 'BAD'}")
                print(f"        viewport={d['vw']}x{d['vh']}  controls={d['controls_w']}px  "
                      f"input={d['input_w']}px (bottom {d['input_bottom']})")
                print(f"        row_fits={d['row_fits']}  input_onscreen={d['input_onscreen']}  "
                      f"dbg_in_header={d['dbg_in_header']} dbg_right={d['dbg_right']} dbg_fits={d['dbg_fits']}")
                print(f"        orb idle={d['orb_idle']} thinking={d['orb_thinking']} "
                      f"radius={d['orb_border_radius']} anim={d['orb_animation']} "
                      f"CIRCLE={d['orb_is_circle']}  spins={spinning} "
                      f"({spin_data.get('distinct')} distinct transforms)")
                for it in d["items"]:
                    print(f"          {it['id']:<14} w={it['w']:>4}  right={it['right']:>4}  '{it['text']}'")

    asyncio.run(run())

    print("\n" + "=" * 62)
    print(f"widths measured: {len(results)}   problems: {failures}")
    print("RESULT:", "PASS" if failures == 0 else "FAIL")
    print("=" * 62)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    import asyncio  # noqa: F401  (used by cdp())
    sys.exit(main())
