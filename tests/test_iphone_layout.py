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
            # The balance check above left the page on another
            # viewport; reload and let it settle before measuring,
            # or the "before" snapshot comes back empty.
            await asyncio.sleep(2.0)

            before_expr = """
            (() => {
              try {
                const t = document.getElementById('mode-toggle');
                if (!t) return JSON.stringify({error: 'no mode-toggle'});
                const b = t.getBoundingClientRect();
                return JSON.stringify({
                  top: Math.round(b.top),
                  visible: b.top >= -1
                           && b.bottom <= window.innerHeight + 1,
                  scrollY: window.scrollY,
                  docScrollTop: document.documentElement.scrollTop,
                  bodyScrollTop: document.body.scrollTop,
                  vh: window.innerHeight,
                  ready: document.readyState,
                });
              } catch (e) {
                return JSON.stringify({error: String(e)});
              }
            })()
            """
            before = await sess("Runtime.evaluate", {
                "expression": before_expr, "returnByValue": True,
            })
            raw = before.get("result", {}).get("value")
            if not raw:
                # A reload can leave Runtime.evaluate in a stale
                # execution context. Reload and try once more.
                await sess("Page.reload", {"ignoreCache": True})
                await asyncio.sleep(1.5)
                before = await sess("Runtime.evaluate", {
                    "expression": before_expr, "returnByValue": True,
                })
                raw = before.get("result", {}).get("value")
                if not raw:
                    print("        before-snapshot failed: "
                          + json.dumps(before)[:180])
            if not raw:
                print("        RAW before-response: "
                      + json.dumps(before)[:300])
            kb_before = json.loads(raw or "{}")
            if kb_before.get("error"):
                print("        before-snapshot error: "
                      + str(kb_before.get("error")))

            # Simulate the keyboard the way iOS ACTUALLY does it.
            #
            # iOS does NOT shrink the layout viewport when the
            # keyboard opens -- it shrinks the VISUAL viewport, and
            # the layout viewport stays full height. The keyboard then
            # covers the bottom of the page.
            #
            # Emulation.setDeviceMetricsOverride shrinks the LAYOUT
            # viewport, which is a different thing -- that is why the
            # earlier version of this test could not tell a correct
            # implementation from a broken one.
            #
            # So: shrink the layout viewport to the visible area
            # (which is what the app's own JS does via
            # visualViewport), AND offset the app by the difference,
            # AND make the layout viewport taller than the visible
            # area, exactly as iOS does.
            await sess("Runtime.evaluate", {
                "expression": """
                (() => {
                  const vv = window.visualViewport;
                  const KEYBOARD = 336;
                  const LAYOUT_H = 852;
                  const VISIBLE_H = LAYOUT_H - KEYBOARD;

                  // iOS: the layout viewport stays 852. The visual
                  // viewport is 852-336 and is scrolled down by the
                  // amount the layout viewport was scrolled.
                  Object.defineProperty(vv, 'height',
                    {configurable: true, get: () => VISIBLE_H});
                  Object.defineProperty(vv, 'offsetTop',
                    {configurable: true, get: () => 0});

                  // The page's own layout is still 852 tall, so the
                  // app must position itself over the visible part.
                  Object.defineProperty(window, 'innerHeight',
                    {configurable: true, get: () => LAYOUT_H});
                  Object.defineProperty(document.documentElement,
                    'clientHeight',
                    {configurable: true, get: () => LAYOUT_H});

                  // The KEYBOARD OPENING is what fires this. In
                  // Chrome, shrinking the layout viewport is the
                  // closest equivalent -- so set the layout viewport
                  // to the FULL height and let the app's own handler
                  // shrink it to the visible height. That is exactly
                  // the iOS sequence: layout stays 852, the app
                  // decides to be 516.
                  window.dispatchEvent(new Event('resize'));
                  vv.dispatchEvent(new Event('resize'));

                  // Now verify the app actually shrank. If the
                  // handler did not run, the app is still 852 tall
                  // and its input is at the bottom -- under the
                  // keyboard.
                  return 'ok';
                })()
                """,
                "returnByValue": True,
            })
            await asyncio.sleep(0.6)

            # ── the REAL send, watched frame by frame ──
            # Three rounds of fixes have failed. Each of them
            # measured the layout at ONE point, before and after.
            # The shake is a TRANSIENT -- it happens in between.
            #
            # So: type into the real input, click the real Send
            # button, and sample every animation frame through the
            # whole turn. Anything that moves is the shake.
            await sess("Emulation.setDeviceMetricsOverride", {
                "width": 393, "height": 852,
                "deviceScaleFactor": 2, "mobile": True,
            })
            await sess("Page.reload", {"ignoreCache": True})
            await asyncio.sleep(2.0)

            watcher = await sess("Runtime.evaluate", {
                "expression": """
                (() => {
                  const ids = {
                    header: '.header',
                    toggle: '#mode-toggle',
                    stage: '#stage',
                    orb: '#orb',
                    shell: '#chat-shell',
                    transcript: '#transcript',
                    controls: '.controls',
                  };
                  const snap = () => {
                    const o = {};
                    for (const k in ids) {
                      const e = document.querySelector(ids[k]);
                      if (!e) { o[k] = null; continue; }
                      const b = e.getBoundingClientRect();
                      o[k] = [Math.round(b.top), Math.round(b.height)];
                    }
                    // Every header child, by height -- the header is a
                    // flex row, so one taller child makes the whole
                    // header taller and pushes everything below it
                    // down.
                    const hdr = document.querySelector('.header');
                    o.kids = [...hdr.children].map(c => {
                      const b = c.getBoundingClientRect();
                      return (c.id || c.className || c.tagName) + ':'
                             + Math.round(b.height);
                    }).join(',');
                    // The status text is what changes on send.
                    o.status = (document.getElementById('status-text')
                      || {}).textContent;
                    // Is the text on the SAME line as the dot? If the
                    // dot is inline-block and the text wraps, the
                    // div is two line-boxes tall.
                    const stEl = document.querySelector('.status');
                    if (stEl) {
                      o.stH = Math.round(stEl.getBoundingClientRect().height);
                      o.stScroll = stEl.scrollHeight;
                      const dot = stEl.querySelector('.dot');
                      const txt = document.getElementById('status-text');
                      if (dot && txt) {
                        const db = dot.getBoundingClientRect();
                        const tb = txt.getBoundingClientRect();
                        o.sameLine = Math.abs(db.top - tb.top) < 4;
                        o.dotTop = Math.round(db.top);
                        o.txtTop = Math.round(tb.top);
                        o.txtW = Math.round(tb.width);
                        o.stW = Math.round(stEl.getBoundingClientRect().width);
                      }
                    }
                    const st = document.querySelector('.status');
                    o.statusW = st
                      ? Math.round(st.getBoundingClientRect().width) : -1;
                    o.scrollY = Math.round(window.scrollY);
                    o.vvH = window.visualViewport
                      ? Math.round(window.visualViewport.height) : -1;
                    o.vvTop = window.visualViewport
                      ? Math.round(window.visualViewport.offsetTop) : -1;
                    o.bodyH = Math.round(
                      document.body.getBoundingClientRect().height);
                    o.htmlH = Math.round(
                      document.documentElement.getBoundingClientRect().height);
                    o.innerH = window.innerHeight;
                    return o;
                  };
                  window.__snaps = [snap()];
                  window.__watching = true;
                  const tick = () => {
                    if (!window.__watching) return;
                    window.__snaps.push(snap());
                    requestAnimationFrame(tick);
                  };
                  requestAnimationFrame(tick);
                  return 'watching';
                })()
                """,
                "returnByValue": True,
            })

            # Type into the real input and click the real button.
            await sess("Runtime.evaluate", {
                "expression": """
                (() => {
                  const inp = document.getElementById('text-input');
                  inp.focus();
                  inp.value = 'this is a test message to trigger the send';
                  return 'typed';
                })()
                """,
                "returnByValue": True,
            })
            await asyncio.sleep(0.4)
            await sess("Runtime.evaluate", {
                "expression": """
                (() => {
                  document.getElementById('send-btn').click();
                  return 'clicked';
                })()
                """,
                "returnByValue": True,
            })
            # Wait for the reply to actually arrive rather than a
            # fixed delay -- the status text changes when the server
            # responds, and on a slow brain that can take a while.
            deadline_send = time.time() + 45
            while time.time() < deadline_send:
                await asyncio.sleep(0.5)
                done_check = await sess("Runtime.evaluate", {
                    "expression": """
                    (() => {
                      const t = document.querySelectorAll('.turn.sam');
                      return t.length;
                    })()
                    """,
                    "returnByValue": True,
                })
                n = done_check.get("result", {}).get("value")
                if isinstance(n, int) and n > 0:
                    break
            await asyncio.sleep(1.0)

            # Stop watching and pull the frames.
            frames = await sess("Runtime.evaluate", {
                "expression": """
                (() => {
                  window.__watching = false;
                  return JSON.stringify(window.__snaps || []);
                })()
                """,
                "returnByValue": True,
            })
            fr = json.loads(frames.get("result", {}).get("value") or "[]")
            print(f"        captured {len(fr)} animation frames")

            # Diff consecutive frames and report every element that
            # moved.
            MOVED = {}
            for i in range(1, len(fr)):
                for k in fr[i]:
                    if fr[i - 1].get(k) != fr[i].get(k):
                        MOVED.setdefault(k, 0)
                        MOVED[k] += 1
            for k, n in sorted(MOVED.items(), key=lambda x: -x[1]):
                first = fr[0].get(k)
                last = fr[-1].get(k)
                print(f"        MOVED {k}: {n} frames, "
                      f"{first} -> {last}")

            # The header children's heights, frame by frame, plus the
            # status text that changes on send.
            print("        the status text and the header's children's "
                  "heights, frame by frame:")
            prev = None
            for i, f in enumerate(fr):
                key = (f.get("status"), f.get("statusW"), f.get("kids"))
                if key != prev:
                    print(f"          frame {i:>3}: status="
                          f"{f.get('status')!r} w={f.get('statusW')} "
                          f"stH={f.get('stH')} scrollH={f.get('stScroll')} "
                          f"dotTop={f.get('dotTop')} txtTop={f.get('txtTop')} "
                          f"txtW={f.get('txtW')} sameLine={f.get('sameLine')}")
                    print(f"                    kids=[{f.get('kids')}]")
                    prev = key
                # The start and end are the SAME -- the element went
                # somewhere and came back. That is the shake, and a
                # start/end comparison cannot see it. Print every
                # distinct value it took.
                seen = []
                for f in fr:
                    v = f.get(k)
                    if not seen or seen[-1] != v:
                        seen.append(v)
                print(f"          path: {seen}")

            # Which HEADER CHILD grows? The header is a flex row; if
            # one of its children gets taller, the whole header does,
            # and everything below it moves down with it.
            kid = await sess("Runtime.evaluate", {
                "expression": """
                (() => {
                  const h = document.querySelector('.header');
                  const out = [];
                  for (const c of h.children) {
                    const b = c.getBoundingClientRect();
                    out.push({
                      tag: c.tagName + (c.id ? '#' + c.id : ''),
                      cls: c.className,
                      h: Math.round(b.height),
                      w: Math.round(b.width),
                      top: Math.round(b.top),
                    });
                  }
                  return JSON.stringify(out);
                })()
                """,
                "returnByValue": True,
            })
            kids = json.loads(kid.get("result", {}).get("value") or "[]")
            print("        header children NOW:")
            for kd in kids:
                print(f"          {kd['tag']:<18} cls={str(kd['cls'])[:16]:<16} "
                      f"h={kd['h']:>3} w={kd['w']:>4} top={kd['top']}")
            if not MOVED:
                print("        nothing moved")

            # ── the device-side watcher must work ──
            # It is a diagnostic tool, so it needs its own test:
            # with ?layoutwatch=1 it must arm, and it must report
            # when something moves. Otherwise it is dead code that
            # looks like a working diagnostic.
            await sess("Emulation.setDeviceMetricsOverride", {
                "width": 393, "height": 852,
                "deviceScaleFactor": 2, "mobile": True,
            })
            await sess("Page.navigate", {
                "url": "http://127.0.0.1:8899/ava_page.html?layoutwatch=1",
            })
            await asyncio.sleep(1.5)

            armed = await sess("Runtime.evaluate", {
                "expression": """
                (() => JSON.stringify({
                  armed: typeof window.__lwMark === 'function',
                }))()
                """,
                "returnByValue": True,
            })
            a3 = json.loads(armed.get("result", {}).get("value") or "{}")
            if not a3.get("armed"):
                failures += 1
            print(f"        the device-side watcher arms: "
                  f"{'OK ' if a3.get('armed') else 'BAD'}")

            # Capture what the watcher REPORTS. The watcher posts
            # through dbg(), so stub that and read the lines.
            await sess("Runtime.evaluate", {
                "expression": """
                (() => {
                  window.__reported = [];
                  const orig = window.dbg;
                  window.dbg = (m) => {
                    if (typeof m === 'string'
                        && m.indexOf('LAYOUTWATCH') === 0) {
                      window.__reported.push(m);
                    }
                    return orig ? orig(m) : undefined;
                  };
                  return 'stubbed';
                })()
                """,
                "returnByValue": True,
            })

            # Move something and confirm the watcher notices.
            detected = await sess("Runtime.evaluate", {
                "expression": """
                (async () => {
                  const before = document.querySelector('#orb-label')
                    .getBoundingClientRect().top;
                  // The watcher records the status text, because
                  // THAT is what broke the layout -- it used to wrap
                  // when it changed from "idle" to "thinking...", which
                  // grew the header and pushed everything down.
                  //
                  // Change the status text and confirm the watcher
                  // notices. The watcher posts to the server, so
                  // instead of watching the log, check that the
                  // watcher's own snapshot function sees the change.
                  // Change the status text -- the exact change that
                  // used to wrap and grow the header -- and let the
                  // watcher notice.
                  const st = document.getElementById('status-text');
                  st.textContent = 'thinking...';
                  await new Promise(r => setTimeout(r, 500));
                  st.textContent = 'idle';
                  await new Promise(r => setTimeout(r, 300));
                  const rep = window.__reported || [];
                  return JSON.stringify({
                    reported: rep.length,
                    sample: rep.length ? rep[rep.length - 1].slice(0, 220)
                                        : '',
                  });
                })()
                """,
                "awaitPromise": True, "returnByValue": True,
            })
            d3 = json.loads(detected.get("result", {}).get("value") or "{}")
            # The watcher must REPORT the change. It posts through
            # dbg(), which the test stubbed, so the reports are
            # captured in window.__reported.
            watcher_ok = d3.get("reported", 0) > 0
            if not watcher_ok:
                failures += 1
            print(f"        the watcher reports a real change: "
                  f"{'OK ' if watcher_ok else 'BAD'} "
                  f"({d3.get('reported')} reports)")
            if d3.get("sample"):
                print(f"          {d3.get('sample')}")

            # Back to the plain page for the rest of the test.
            await sess("Page.navigate", {
                "url": "http://127.0.0.1:8899/ava_page.html",
            })
            await asyncio.sleep(1.2)

            # The verdict on the real send: NOTHING may move.
            # The header is the first grid row, so if it grows, every
            # row below it moves down -- that is the shake.
            shake = {}
            for k in ("header", "toggle", "stage", "orb", "shell",
                      "transcript", "controls"):
                vals = [f.get(k) for f in fr if f.get(k) is not None]
                if not vals:
                    continue
                tops = {v[0] for v in vals}
                heights = {v[1] for v in vals}
                if len(tops) > 1 or len(heights) > 1:
                    shake[k] = (sorted(tops), sorted(heights))
            if shake:
                failures += 1
                for k, (tops, heights) in shake.items():
                    print(f"        SHAKE: {k} moved -- "
                          f"tops={tops} heights={heights}")
            else:
                print("        NOTHING MOVED during a real send")

            # ── the KEYBOARD CLOSING, from the screen recording ──
            # The recording showed: keyboard open -> Send -> keyboard
            # closes -> the orb's bright peak moves 197 -> 277 -> 286
            # -> 319 -> 418. The app was sized to the visual viewport,
            # so closing the keyboard re-laid-out the whole page.
            #
            # Now the app is always full-screen, so this must move
            # NOTHING except the input row.
            await sess("Emulation.setDeviceMetricsOverride", {
                "width": 393, "height": 852,
                "deviceScaleFactor": 2, "mobile": True,
            })
            await sess("Page.reload", {"ignoreCache": True})
            await asyncio.sleep(1.5)

            kb = await sess("Runtime.evaluate", {
                "expression": """
                (async () => {
                  const vv = window.visualViewport;
                  const KEYBOARD = 336;
                  const LAYOUT_H = window.innerHeight;
                  const orb = () => document.getElementById('orb')
                    .getBoundingClientRect();
                  const inp = () => document.getElementById('text-input')
                    .getBoundingClientRect();
                  const snap = (label) => ({
                    label: label,
                    orbTop: Math.round(orb().top),
                    orbH: Math.round(orb().height),
                    toggleTop: Math.round(
                      document.getElementById('mode-toggle')
                        .getBoundingClientRect().top),
                    stageH: Math.round(
                      document.getElementById('stage')
                        .getBoundingClientRect().height),
                    headerH: Math.round(
                      document.querySelector('.header')
                        .getBoundingClientRect().height),
                    inputBottom: Math.round(inp().bottom),
                    layoutH: LAYOUT_H,
                    visibleH: Math.round(vv.height),
                    kbH: LAYOUT_H - Math.round(vv.height),
                  });

                  const out = [];

                  // 1. keyboard OPEN
                  Object.defineProperty(vv, 'height',
                    {configurable: true, get: () => LAYOUT_H - KEYBOARD});
                  vv.dispatchEvent(new Event('resize'));
                  await new Promise(r => setTimeout(r, 350));
                  out.push(snap('keyboard-open'));

                  // 2. keyboard CLOSED (what the blur used to cause)
                  Object.defineProperty(vv, 'height',
                    {configurable: true, get: () => LAYOUT_H});
                  vv.dispatchEvent(new Event('resize'));
                  await new Promise(r => setTimeout(r, 350));
                  out.push(snap('keyboard-closed'));

                  return JSON.stringify(out);
                })()
                """,
                "awaitPromise": True, "returnByValue": True,
            })
            kbd = json.loads(kb.get("result", {}).get("value") or "[]")
            if len(kbd) == 2:
                a, b = kbd[0], kbd[1]
                for key in ("orbTop", "orbH", "toggleTop", "stageH",
                            "headerH"):
                    same = a.get(key) == b.get(key)
                    if not same:
                        failures += 1
                    print(f"        {key:<12} keyboard open="
                          f"{a.get(key)}  closed={b.get(key)}  "
                          f"{'OK ' if same else 'BAD'}")
                print(f"        the input row rides above the keyboard: "
                      f"open_bottom={a.get('inputBottom')} "
                      f"(visible ends at {a.get('visibleH')})")
            else:
                failures += 1
                print(f"        BAD: only got {len(kbd)} snapshots")

            # ── the flicker: the transcript must not scroll BACK ──
            # The screen recording showed the conversation appearing
            # once in one frame and twice in the next, over and over.
            # That is the transcript scrolling up and down: two turns,
            # two opposite scroll decisions.
            #
            # The cause was the stick-to-bottom check reading
            # scrollHeight and clientHeight at different moments, so
            # the answer flipped between turns.
            await sess("Emulation.setDeviceMetricsOverride", {
                "width": 393, "height": 852,
                "deviceScaleFactor": 2, "mobile": True,
            })
            await sess("Page.reload", {"ignoreCache": True})
            await asyncio.sleep(1.5)

            flick = await sess("Runtime.evaluate", {
                "expression": """
                (async () => {
                  const tr = document.getElementById('transcript');
                  const empty = tr.querySelector('.empty');
                  if (empty) empty.remove();
                  const positions = [];
                  // Add turns the way sendText does.
                  for (let i = 0; i < 6; i++) {
                    const t = document.createElement('div');
                    t.className = 'turn user';
                    t.innerHTML = '<div class="role">you</div>'
                      + '<div class="text">Test message number '
                      + i + ' with some length to it</div>';
                    tr.appendChild(t);
                    const atBottomBefore = tr.scrollHeight - tr.scrollTop
                                           - tr.clientHeight;
                    const stick = atBottomBefore < 40;
                    // Scroll the way addTurn does, THEN measure.
                    if (stick) {
                      tr.scrollTop = tr.scrollHeight;
                    }
                    // scroll-behavior:smooth animates the scroll, so
                    // wait for it to SETTLE rather than guessing a
                    // duration -- 120ms was sometimes not enough and
                    // the test caught it mid-animation.
                    let lastTop = -1, still = 0;
                    for (let k = 0; k < 40; k++) {
                      await new Promise(r => setTimeout(r, 25));
                      const t = Math.round(tr.scrollTop);
                      if (t === lastTop) { if (++still >= 3) break; }
                      else { still = 0; lastTop = t; }
                    }
                    positions.push({
                      scrollTop: Math.round(tr.scrollTop),
                      max: Math.round(tr.scrollHeight - tr.clientHeight),
                      wasAtBottom: stick,
                    });
                  }
                  return JSON.stringify(positions);
                })()
                """,
                "awaitPromise": True, "returnByValue": True,
            })
            pos = json.loads(flick.get("result", {}).get("value") or "[]")
            # After every turn, scrollTop should be at the maximum
            # (we started at the bottom, so we should stay there).
            # If it is ever less than the max, the view jumped.
            # The scroll position must never DECREASE, and when we
            # were at the bottom it must end at the bottom.
            backwards = []
            for i, p in enumerate(pos):
                if i and p["scrollTop"] < pos[i - 1]["scrollTop"]:
                    backwards.append(p)
                if p["wasAtBottom"] and p["max"] > 0 \
                        and p["scrollTop"] < p["max"] - 2:
                    backwards.append(p)
            if backwards:
                failures += 1
            print(f"        transcript scroll positions: "
                  f"{[(p['scrollTop'], p['max']) for p in pos]}")
            print(f"        the transcript never scrolls BACK: "
                  f"{'OK ' if not backwards else 'BAD'}")

            # The transcript must actually SCROLL. scrollTop stayed 0
            # while the content grew past the box, so the user cannot
            # read the latest message.
            box = await sess("Runtime.evaluate", {
                "expression": """
                (() => {
                  const tr = document.getElementById('transcript');
                  const cs = getComputedStyle(tr);
                  const shell = document.getElementById('chat-shell');
                  const scs = getComputedStyle(shell);
                  return JSON.stringify({
                    trH: Math.round(tr.getBoundingClientRect().height),
                    trScrollH: tr.scrollHeight,
                    trClientH: tr.clientHeight,
                    overflowY: cs.overflowY,
                    trFlex: cs.flex,
                    trMinH: cs.minHeight,
                    trMaxH: cs.maxHeight,
                    shellH: Math.round(shell.getBoundingClientRect().height),
                    shellFlex: scs.flex,
                    shellMinH: scs.minHeight,
                    shellDisplay: scs.display,
                  });
                })()
                """,
                "returnByValue": True,
            })
            bx = json.loads(box.get("result", {}).get("value") or "{}")
            scroll_ok = (bx.get("trScrollH", 0) > bx.get("trClientH", 0)
                         and bx.get("overflowY") == "auto")
            if not scroll_ok:
                failures += 1
            print(f"        the transcript box: "
                  f"h={bx.get('trH')} scrollH={bx.get('trScrollH')} "
                  f"clientH={bx.get('trClientH')} "
                  f"overflowY={bx.get('overflowY')} "
                  f"flex={bx.get('trFlex')} maxH={bx.get('trMaxH')} "
                  f"shellH={bx.get('shellH')} shellFlex={bx.get('shellFlex')}")
            print(f"        the transcript can scroll: "
                  f"{'OK ' if scroll_ok else 'BAD'}")

            # ── the balance check ──
            # The conversation area now takes a fixed 45vh, so the
            # stage gets the rest. The orb must still be a sensible
            # size, and the transcript must have room to show text.
            for w, h, label in WIDTHS:
                await sess("Emulation.setDeviceMetricsOverride", {
                    "width": w, "height": h,
                    "deviceScaleFactor": 2, "mobile": True,
                })
                await sess("Page.reload", {"ignoreCache": True})
                await asyncio.sleep(0.8)
                bal = await sess("Runtime.evaluate", {
                    "expression": """
                    (() => {
                      const orb = document.querySelector('.orb')
                        .getBoundingClientRect();
                      const tr = document.getElementById('transcript')
                        .getBoundingClientRect();
                      const inp = document.getElementById('text-input')
                        .getBoundingClientRect();
                      return JSON.stringify({
                        orb: Math.round(orb.height),
                        tr: Math.round(tr.height),
                        inpW: Math.round(inp.width),
                      });
                    })()
                    """,
                    "returnByValue": True,
                })
                bal_data = json.loads(
                    bal.get("result", {}).get("value") or "{}")
                bal_ok = (bal_data.get("orb", 0) >= 100
                          and bal_data.get("tr", 0) >= 80
                          and bal_data.get("inpW", 0) >= 100)
                if not bal_ok:
                    failures += 1
                print(f"        {w:>4}px {label:<22} "
                      f"orb={bal_data.get('orb'):>3} "
                      f"transcript={bal_data.get('tr'):>3} "
                      f"input={bal_data.get('inpW'):>3}  "
                      f"{'OK ' if bal_ok else 'BAD'}")

            # ── the "shakes up and down after I hit send" check ──
            # Send does two things that change the layout:
            #   1. addTurn() adds a turn -- the shell row is
            #      `minmax(0, auto)`, so it GROWS, the stage row
            #      (1fr) shrinks, and everything above the transcript
            #      moves up.
            #   2. setState("thinking") changes the orb-label text
            #      from "tap to talk" to "thinking...".
            #
            # The top of the screen must NOT move. Measure the
            # toggle's top and the header's bottom before and after
            # a simulated send.
            await sess("Emulation.setDeviceMetricsOverride", {
                "width": 393, "height": 852,
                "deviceScaleFactor": 2, "mobile": True,
            })
            await sess("Page.reload", {"ignoreCache": True})
            await asyncio.sleep(1.2)

            async def snapshot():
                r = await sess("Runtime.evaluate", {
                    "expression": """
                    (() => {
                      const t = document.getElementById('mode-toggle');
                      const h = document.querySelector('.header');
                      const st = document.getElementById('stage');
                      const lbl = document.getElementById('orb-label');
                      const tr = document.getElementById('transcript');
                      return JSON.stringify({
                        toggleTop: Math.round(t.getBoundingClientRect().top),
                        headerH: Math.round(h.getBoundingClientRect().height),
                        stageH: Math.round(st.getBoundingClientRect().height),
                        labelH: Math.round(lbl.getBoundingClientRect().height),
                        labelText: (lbl.textContent || '').trim(),
                        shellH: Math.round(document.getElementById('chat-shell')
                                            .getBoundingClientRect().height),
                        trH: Math.round(tr.getBoundingClientRect().height),
                      });
                    })()
                    """,
                    "returnByValue": True,
                })
                return json.loads(r.get("result", {}).get("value") or "{}")

            before_snap = await snapshot()

            # Simulate exactly what the Send button does.
            await sess("Runtime.evaluate", {
                "expression": """
                (() => {
                  const tr = document.getElementById('transcript');
                  const empty = tr.querySelector('.empty');
                  if (empty) empty.remove();
                  const turn = document.createElement('div');
                  turn.className = 'turn user';
                  turn.innerHTML = '<div class="role">you</div>'
                    + '<div class="text">a test message that was typed</div>';
                  tr.appendChild(turn);
                  const orb = document.querySelector('.orb');
                  const lbl = document.getElementById('orb-label');
                  lbl.textContent = 'thinking...';
                  orb.className = 'orb thinking';
                  return 'ok';
                })()
                """,
                "returnByValue": True,
            })
            await asyncio.sleep(0.4)

            after_snap = await snapshot()

            toggle_moved = abs(after_snap.get("toggleTop", 0)
                               - before_snap.get("toggleTop", 0))
            label_changed = (after_snap.get("labelH", 0)
                             != before_snap.get("labelH", 0))
            # The STAGE must not change height either. The orb is
            # anchored to the stage's top, so a stage that grows or
            # shrinks by even a few pixels moves the orb -- and that
            # is the "shakes up and down" report.
            stage_moved = abs(after_snap.get("stageH", 0)
                              - before_snap.get("stageH", 0))
            no_shake = (toggle_moved == 0 and stage_moved == 0
                        and not label_changed)

            if not no_shake:
                failures += 1
            print(f"        before: toggleTop={before_snap.get('toggleTop')} "
                  f"stageH={before_snap.get('stageH')} "
                  f"labelH={before_snap.get('labelH')} "
                  f"shellH={before_snap.get('shellH')}")
            print(f"        after:  toggleTop={after_snap.get('toggleTop')} "
                  f"stageH={after_snap.get('stageH')} "
                  f"labelH={after_snap.get('labelH')} "
                  f"shellH={after_snap.get('shellH')}")
            print(f"        the top of the screen does not move: "
                  f"{'OK ' if no_shake else 'BAD'} "
                  f"(toggle moved {toggle_moved}px, stage moved "
                  f"{stage_moved}px, label height changed={label_changed})")
            if label_changed:
                print(f"        NOTE: the orb-label height changed "
                      f"({before_snap.get('labelH')} -> "
                      f"{after_snap.get('labelH')}) -- that alone can "
                      f"move the stage contents")

            # ── the "glitch when I click send" check ──
            # The orb's `transition: all 0.3s ease` animates EVERY
            # property -- including `transform`, which is owned by the
            # pulse/spin keyframes. Switching the animation class
            # (which setState does on every state change) animates the
            # transform from the old animation's end state back to
            # identity. That is a visible jump.
            #
            # The fix is to transition only the properties that should
            # animate: the colours and the glow. `transform` belongs
            # to the keyframes alone.
            await sess("Emulation.setDeviceMetricsOverride", {
                "width": 393, "height": 852,
                "deviceScaleFactor": 2, "mobile": True,
            })
            await sess("Page.reload", {"ignoreCache": True})
            await asyncio.sleep(1.2)

            anim = await sess("Runtime.evaluate", {
                "expression": """
                (() => {
                  const orb = document.querySelector('.orb');
                  const cs = getComputedStyle(orb);
                  const props = cs.transitionProperty || '';
                  return JSON.stringify({
                    transitionProperty: props,
                    transitionsTransform: /(^|,|\s)transform(\s|,|$)/.test(props),
                    transitionAll: /(^|,|\s)all(\s|,|$)/.test(props),
                    duration: cs.transitionDuration,
                  });
                })()
                """,
                "returnByValue": True,
            })
            a2 = json.loads(anim.get("result", {}).get("value") or "{}")

            anim_ok = (not a2.get("transitionsTransform", True)
                       and not a2.get("transitionAll", True))
            if not anim_ok:
                failures += 1
            print(f"        orb transition: property="
                  f"'{a2.get('transitionProperty')}' "
                  f"duration={a2.get('duration')}")
            print(f"        the orb does not animate transform: "
                  f"{'OK ' if anim_ok else 'BAD'}")

            # ── the decisive check ──
            # The layout viewport is 852 (iOS keeps it that way).
            # The visible area is 516. The app's body must therefore
            # be 516 tall and start at y=0, so the input lands
            # INSIDE the visible band.
            #
            # If the app were left at the layout height (852), its
            # input would be at y~780 -- under the keyboard. That is
            # the bug being fixed.
            geom = await sess("Runtime.evaluate", {
                "expression": """
                (() => {
                  const st = getComputedStyle(document.body);
                  const inp = document.getElementById('text-input');
                  const ib = inp.getBoundingClientRect();
                  const vv = window.visualViewport;
                  return JSON.stringify({
                    bodyH: Math.round(parseFloat(st.height)),
                    bodyTop: Math.round(parseFloat(st.top)),
                    inputBottom: Math.round(ib.bottom),
                    visibleH: Math.round(vv ? vv.height : window.innerHeight),
                    layoutH: window.innerHeight,
                  });
                })()
                """,
                "returnByValue": True,
            })
            g = json.loads(geom.get("result", {}).get("value") or "{}")

            # The app must be no taller than the visible area.
            app_fits_visible = g.get("bodyH", 99999) <= g.get("visibleH", 0) + 1
            # And the input must be inside the visible band.
            input_above_kbd = g.get("inputBottom", 99999) <= g.get("visibleH", 0) + 1

            if not (app_fits_visible and input_above_kbd):
                failures += 1
            print(f"        app: bodyH={g.get('bodyH')} top={g.get('bodyTop')} "
                  f"inputBottom={g.get('inputBottom')} "
                  f"visibleH={g.get('visibleH')} layoutH={g.get('layoutH')}")
            print(f"        app fits the visible area: "
                  f"{'OK ' if app_fits_visible else 'BAD'}   "
                  f"input above the keyboard: "
                  f"{'OK ' if input_above_kbd else 'BAD'}")

            after = await sess("Runtime.evaluate", {
                "expression": """
                (() => {
                  const t = document.getElementById('mode-toggle');
                  const b = t.getBoundingClientRect();
                  const st = getComputedStyle(document.body);
                  const inp = document.getElementById('text-input');
                  const ib = inp ? inp.getBoundingClientRect() : null;
                  // The VISIBLE area. On iOS the keyboard eats the
                  // bottom of the screen, so "above the keyboard"
                  // means "inside the visual viewport", NOT "inside
                  // window.innerHeight" -- the layout viewport is
                  // still full height while the keyboard is up.
                  const visibleH = window.visualViewport
                    ? window.visualViewport.height
                    : window.innerHeight;
                  return JSON.stringify({
                    top: Math.round(b.top),
                    visible: b.top >= -1 && b.bottom <= visibleH + 1,
                    scrollY: window.scrollY,
                    bodyPosition: st.position,
                    bodyHeight: st.height,
                    bodyTop: st.top,
                    vh: window.innerHeight,
                    visibleH: Math.round(visibleH),
                    // THE POINT: the input must be INSIDE the visible
                    // area, i.e. ABOVE the keyboard, not under it.
                    input_bottom: ib ? Math.round(ib.bottom) : null,
                    input_visible: ib
                      ? (ib.bottom <= visibleH + 1 && ib.top >= -1)
                      : false,
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
                     and abs(a.get("top", 0)
                             - kb_before.get("top", 0)) <= 2
                     # the input must be ABOVE the keyboard, not under it
                     and a.get("input_visible") is True
                     and a.get("input_bottom", 99999)
                         <= a.get("visibleH", 0) + 1)
            if not kb_ok:
                failures += 1
            print(f"        before: top={kb_before.get('top')} "
                  f"visible={kb_before.get('visible')} "
                  f"scrollY={kb_before.get('scrollY')} "
                  f"vh={kb_before.get('vh')}")
            print(f"        after:  top={a.get('top')} visible={a.get('visible')} "
                  f"scrollY={a.get('scrollY')} body={a.get('bodyPosition')} "
                  f"h={a.get('bodyHeight')} top={a.get('bodyTop')} vh={a.get('vh')}")
            print(f"        input:  bottom={a.get('input_bottom')} "
                  f"visible={a.get('input_visible')} "
                  f"(must be <= visibleH {a.get('visibleH')} to be above "
                  f"the keyboard; layout vh is {a.get('vh')})")
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
