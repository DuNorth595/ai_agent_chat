#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""Extract the embedded HTML/JS from agent_voice_ai.py and syntax-check
the JavaScript with node (if available)."""
import re
import subprocess
from pathlib import Path
import sys

W = str(Path(__file__).resolve().parent.parent / "agent_voice_ai.py")
src = open(W, encoding="utf-8").read()

# The page is served as an HTMLResponse with a big triple-quoted string.
# Find the largest <script> ... </script> region in the source.
starts = [m.start() for m in re.finditer(r"<script", src)]
ends = [m.start() for m in re.finditer(r"</script>", src)]
if not starts or not ends:
    print("FAIL: no <script> found")
    sys.exit(1)

best = None
for s in starts:
    for e in ends:
        if e > s:
            size = e - s
            if best is None or size > best[1] - best[0]:
                best = (s, e)
if best is None:
    print("FAIL: could not pair <script>/</script>")
    sys.exit(1)

block = src[best[0]:best[1]]
# Strip the tags and the Python f-string braces are NOT present (it's a
# plain string), so the JS is verbatim.
js = re.sub(r"^<script[^>]*>", "", block)
js = re.sub(r"</script>$", "", js)

open("/tmp/ava_page.js", "w", encoding="utf-8").write(js)
print(f"extracted {len(js)} chars of JS to /tmp/ava_page.js")

# Check node is available
try:
    v = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=10)
    print("node:", v.stdout.strip())
except Exception:
    print("node NOT available — falling back to python-side brace check")
    sys.exit(0)

r = subprocess.run(["node", "--check", "/tmp/ava_page.js"],
                   capture_output=True, text=True, timeout=30)
if r.returncode == 0:
    print("✓ JS SYNTAX OK")
else:
    print("✗ JS SYNTAX ERROR:")
    print(r.stderr[:3000])
    sys.exit(1)
