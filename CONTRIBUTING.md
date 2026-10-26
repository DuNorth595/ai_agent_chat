# Contributing

Thanks for considering a contribution to `ai_agent_chat`. This is a small, focused project — the contribution surface is intentionally narrow.

## What kind of contributions are useful

- **Bug reports** with reproduction steps (a failing test is the best case).
- **Wire-protocol improvements** — the `/ws` endpoint is the core of this project. Any change to message shape, audio format, or state machine is high-impact.
- **iOS Safari compatibility fixes** — Safari is the most restrictive browser we support; almost any improvement here is welcome.
- **i18n / TTS voice models** — Piper supports dozens of voices; PRs that document a working voice + sample audio are useful.
- **Documentation clarifications** — README, SECURITY.md, SYNC.md.

## What kind of contributions are NOT useful

- **Drive-by rewrites.** This codebase has been deliberately shaped. If you want to swap out FastAPI for something else, open an issue first; don't just PR it.
- **Reformatting / renaming for style preference.** `ruff` is the source of truth. Run it before committing.
- **Adding new features without a use case.** The roadmap (`CHANGELOG.md` → `[unreleased]` → `### Planned`) is the queue. Open an issue and add to the roadmap; don't surprise the person running it.

## Development setup

```bash
# 1. Clone
git clone https://github.com/DuNorth595/ai_agent_chat.git
cd ai_agent_chat

# 2. venv
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Env
cp .env.example .env
# Edit .env — at minimum, set BRAIN_API_KEY.

# 4. Run external services (whisper.cpp, piper, ffmpeg)
# See README.md §"Install" → "External services"

# 5. Run the bridge
python agent_voice_ai.py
# Visit https://127.0.0.1:8770/ in your browser.
```

## Code style

- **Python 3.11+** — match the runtime target.
- **`ruff`** with the rules in `pyproject.toml` (`E`, `F`, `I`, `B`, `UP`).
- **Type hints** on all public functions.
- **No `print()`** for runtime output — use `logger`. `print()` is fine in `__main__` scripts but should not appear in library code.
- **No `from foo import *`.** Always import the names you use.

## Commit message format

Conventional Commits, scoped:

```
<type>(<scope>): <subject>

<body>

<footer>
```

- `type` ∈ `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `perf`
- `scope` = the file or subsystem affected, lowercase (`brain`, `piper`, `ws`, `secrets`, `telegram`)
- `subject` = imperative mood, no period, ≤ 72 chars
- `body` = what changed and why, wrapped at 72 chars
- `footer` = `Refs:` / `Closes:` issue numbers, `BREAKING CHANGE:` notes

Examples from this project's history:
- `feat(ws): sentence-streaming TTS — first audio chunk lands while later sentences are still being synthesized`
- `fix(client): iOS Safari AbortError / NotAllowedError silent spin — set _wsWaitingForGesture and surface tap-to-unlock cue`
- `chore: PII scrub — redact absolute path and chat ID from docs`

## Pull request process

1. Open an issue first for non-trivial changes. Reference it in the PR.
2. Branch from `main`.
3. Run `ruff check .` and `pytest tests/` (when tests exist).
4. Update `CHANGELOG.md` under `[unreleased]` for any user-visible change.
5. Update `README.md` if the change affects install / config / wire protocol.
6. Re-read [SYNC.md](SYNC.md) — the pre-release checklist applies to PRs that bump version.
7. Self-review your diff with `git diff main…HEAD`. If a line doesn't answer "why is this here?", remove it.
8. Squash-merge is fine; the maintainer will rewrite the commit message if needed.

## Secrets

**Do not commit a secret.** See [SECURITY.md](SECURITY.md) for the full policy. The two-line version:

- `.env` is gitignored. Use `.env.example` for placeholders.
- If your PR accidentally includes a key, **rotate the key immediately** and open a private security advisory on the repository — don't try to edit it out of history.

## Code of conduct

Be kind. Assume good faith. Disagree with the idea, not the person. This is a small project; we have time for each other.

## Questions?

Open one. Issues are the right channel.

---

_This file is modeled on the [GitHub Open Source Guides](https://opensource.guide/building-community/)._