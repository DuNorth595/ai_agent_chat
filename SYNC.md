# SYNC.md — Doc / Test / Version Sync Checklist for `ai_agent_chat`

**Use this checklist before tagging any release.** A release is a version bump in `agent_voice_ai.py`, `setup.py` (if added), `README.md`, and `CHANGELOG.md` — and they all have to agree. Drift between docs and code is the #1 way a release ships broken.

---

## Pre-release checklist

For each release (patch, minor, major), confirm:

### 1. Version numbers match
- [ ] `agent_voice_ai.py` `__version__` = release version (e.g. `0.9.1`)
- [ ] `README.md` Cover Page → "**Version**" row = same
- [ ] `CHANGELOG.md` → newest heading = `## [X.Y.Z] — YYYY-MM-DD`
- [ ] If `pyproject.toml` exists → `version` field matches
- [ ] `requirements.txt` → no version drift on `fastapi`, `uvicorn`, `requests`, `numpy`

### 2. CHANGELOG is current
- [ ] New version section exists with subsections: `### Added`, `### Changed`, `### Fixed`, `### Removed`, `### Notes`
- [ ] Every entry references either a commit SHA or a CHANGELOG-only entry with no SHA
- [ ] Unreleased section is empty (or moved into the new version section)

### 3. README reflects reality
- [ ] "Features" section lists every feature that actually works (no vapor)
- [ ] "Install" section is reproducible on a clean machine (last verified: <date>)
- [ ] "For another agent — picking up this codebase" section at the bottom is still accurate (file paths, env vars, wire protocol)
- [ ] No `TODO`, `FIXME`, or "coming soon" text unless it's under `## [unreleased]` → `### Planned`

### 4. Code is clean
- [ ] No `print()` debug statements left in source
- [ ] No commented-out code blocks (`# old: ...`)
- [ ] No `.backup-*` or `.bak-pre-*` files in repo root
- [ ] No `__pycache__/` directories
- [ ] `grep -r "TODO\|FIXME\|XXX" agent_voice_ai.py brain.py piper_tts.py whisper_stt.py tools.py` returns nothing blocking

### 5. Security audit
- [ ] `grep -r "sk-ant-\|sk-live-\|sk-test-\|bot_token\|BEGIN.*PRIVATE.*KEY" . --exclude-dir=.venv --exclude-dir=.git` returns nothing
- [ ] `cat .env 2>/dev/null` — `.env` does not exist (it's `.gitignore`'d, but verify)
- [ ] `ls certs/` shows only `server.crt` + `server.key` placeholder, or the directory is absent
- [ ] No Tailscale / hostname / phone number / personal email in any file

### 6. Tests (when they exist)
- [ ] `pytest tests/` is green
- [ ] No skipped tests unless intentionally behind a `RUN_LIVE=1` gate
- [ ] Coverage on `tools.py` (tiered confirmation protocol) ≥ 80%

### 7. launchd plist matches reality
- [ ] `examples/com.agent-voice-ai.bridge.plist` has the current version label
- [ ] `launchctl list | grep agent-voice-ai` shows the running service (the launchd label is unchanged)
- [ ] `curl -k https://127.0.0.1:8770/` returns HTML with the new version stamp

---

## Drift signals to watch for

These are the patterns that have caused real bugs in this project's history. Add to this list when you find new ones:

- **`iris` / `IRIS` reappearing in source.** The rename was deliberate; any reappearance is a regression. Grep for `iris` in any new file before commit.
- **`pkill iris` / `pkill agent-voice-ai` confusion.** The launchd label, process name, and python module all need to stay in sync. If you rename one, rename all three in the same commit.
- **"Server is up but client shows old version."** 99% of the time: browser cache. Hard-refresh first; if the bug persists, check the served HTML with `curl` and grep for the version stamp.
- **"Patch silently didn't land."** `write_file` failures are silent in some pipelines. After any patch, run `git diff` and visually verify the change is there.
- **"v0.9 shipped" but HTML still says v0.8.** Two files holding version: the JS that sets `const VERSION` and the HTML fallback. Both must update.

---

## Post-release checklist

After tagging + pushing:

- [ ] GitHub release created with `CHANGELOG.md` excerpt
- [ ] Tarball / zip attached (or `git archive` mentioned in release notes)
- [ ] No follow-up "oops, missed a file" commits within 24 hours (this is a release-quality signal — if it happens, the pre-release checklist needs more items)

---

**Maintenance rule:** when this checklist proves insufficient (a release shipped with X broken and X wasn't checked here), add X as a new item. The list should grow monotonically.