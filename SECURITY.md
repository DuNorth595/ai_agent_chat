# Security Policy

## Reporting a vulnerability

**Do not file a public GitHub issue for a security bug.**

Open a private security advisory on the repository, or open a regular
issue that describes the *shape* of the problem without exploit
details, and ask for details to be moved to a private channel.

Please include:

- Reproduction steps (a failing test is ideal)
- Impact assessment — what can an attacker actually do?
- Whether the reproduction needs network access or is local-only
- Any logs that show the issue (**redact API keys and seeds**)

## Threat model

`ai_agent_chat` is a **single-user server** that bridges a browser
microphone to an LLM API. It does not run third-party code on your
behalf and it does not custody credentials.

**The bridge has no authentication.** Anything that can reach the port
can use your LLM key, read the served page, and drive the agent's tool
surface. Network-level access control *is* the authentication model.

This means:

| Deployment | Verdict |
|---|---|
| `localhost`, single machine | ✅ Fine |
| Tailnet / VPN (Tailscale, WireGuard) | ✅ Fine — this is the intended deployment |
| Trusted LAN | ⚠️ Acceptable, but the cert warning is friction on iOS |
| Port-forwarded to the internet | ❌ **Never.** Use a VPN or an authenticating proxy. |

The default bind address is `0.0.0.0` so the bridge is reachable from
a VPN interface. If you want it locked to one machine, set
`AVA_HOST=127.0.0.1`.

## Asset inventory

| Asset | Where it lives | What the bridge does with it |
|---|---|---|
| LLM API key | Operator's `.env` | Read via `os.environ`. **Never logged, never written to disk, never included in error tracebacks.** |
| Telegram bot token | Operator's `.env` | Read via `os.environ`. Used only by `telegram_sync.py`. **Never logged.** |
| Telegram chat id | Operator's `.env` | A destination address, not a secret, but still shouldn't be committed. |
| TLS private key | `certs/server.key` | Used by uvicorn to terminate HTTPS. **Never logged.** Never leaves the host. |
| User audio | Browser → WebSocket → STT → LLM | Transient. Audio bytes are transcribed and discarded; they are **not persisted** by default. |
| Transcripts | The LLM provider's API logs | Subject to the provider's retention policy. **Not stored by this bridge.** |
| Browser state | `localStorage` on the device | UI preferences only. Not a secret. |

## Secret-handling rules for contributors

1. **Never log a secret.** `print(api_key)`, `logger.info(token)`,
   `repr(env_dict)` — all forbidden. If you need correlation, log a
   length and a suffix:
   ```python
   key = os.environ["BRAIN_API_KEY"]
   logger.info("api_key loaded (%d chars, suffix=…%s)", len(key), key[-4:])
   ```

2. **Never commit a secret.** Two layers of defense:
   - **`.gitignore`** excludes `.env`, `certs/`, `*.pem`, `*.key`,
     `*.crt`. Before every commit:
     ```bash
     git status --ignored | grep -E '\.env$|\.key$|\.pem$'
     ```
   - **Manual review** of `git diff --cached` for anything token-shaped:
     `sk-…`, a long numeric `chat_id` followed by `:AA…`,
     `-----BEGIN … PRIVATE KEY-----`.
   - For a test secret, generate one inside the test:
     ```python
     os.environ["TEST_FAKE_KEY"] = "test_" + secrets.token_hex(16)
     ```

3. **Use environment variables, never literals.**
   `os.environ["BRAIN_API_KEY"]`, not
   `client = Anthropic(api_key="sk-...")`.

4. **No secrets in error messages.** When wrapping an exception, log
   `e.args[0]` — the human-readable message — not the exception
   object. Upstream libraries sometimes include the request body in
   tracebacks.

5. **Don't try to scrub process memory.** Python strings are
   immutable. Minimize a secret's lifetime instead: load it, use it,
   drop the reference.

6. **No real infrastructure identifiers in code or tests.** No
   personal IPs, hostnames, account names, or tailnet addresses. Use
   placeholders like `100.64.0.1` (the Tailscale CGNAT range) and
   `example.com`. The test suite resolves binaries and models
   dynamically — see `tests/_testcfg.py`.

## Cryptographic choices

- **Transport encryption:** TLS 1.2+ via uvicorn. Certificate and key
  are operator-supplied. No custom crypto, no custom protocol.
- **WebSocket auth:** none at the protocol level. This is deliberate —
  the deployment model assumes a trusted network. See
  [Known limitations](#known-limitations) #1.
- **No end-to-end encryption.** Audio and text are plaintext inside
  the TLS tunnel to the LLM provider. The LLM has to read the prompt
  to answer.
- **No message signing.** Telegram messages go through the official
  Bot API over HTTPS.

## Dependencies

Runtime: `fastapi`, `uvicorn`, `requests`, `numpy`.

| Type | What it adds | Why it's optional |
|---|---|---|
| Optional (Python) | `kokoro`, `soundfile` | Only if you swap Piper for Kokoro. |
| Optional (system) | `whisper.cpp` | External STT server, called over HTTP. |
| Optional (system) | `piper` | External TTS binary, invoked as a subprocess. |
| Optional (system) | `ffmpeg` | Required for `audio/webm` → `audio/wav` transcoding. |

`requirements.txt` pins major-version floors but not patch versions.
For supply-chain hardening, run `pip-audit` against your environment.

## Known limitations

1. **No authentication on the bridge or the WebSocket.** Anyone who
   can reach the port can drive the agent and spend your LLM credits.
   *Mitigation:* keep it on a VPN/tailnet, or front it with a
   reverse proxy that does auth.

2. **No rate limiting.** A client can spam requests and rack up LLM
   costs. *Mitigation:* network-level access control.

3. **No CSRF protection on the page.** The page has no state to
   protect today, but if you add settings persistence in a fork,
   defend it with SameSite cookies and a custom request header.

4. **Audio isn't persisted, but the LLM provider's retention applies.**
   If you need stricter retention than your provider offers, use a
   self-hosted model or configure the provider accordingly.

5. **The launchd plist runs as your user.** If the bridge is
   compromised, the attacker has your permissions. *Mitigation:* run it
   as a low-privilege service account.

6. **CORS defaults to `*`.** Fine for a single-user local install.
   Tighten `AVA_CORS_ORIGINS` if the bridge is reachable from a
   shared network.

7. **The tool surface can act on your behalf.** If you enable the
   Telegram tool, the agent can send messages. Irreversible external
   actions are held for confirmation unless a pre-approval phrase is
   used — but pre-approval phrases are deliberately easy to say out
   loud. *Mitigation:* think about what you enable, and don't enable
   the Telegram tool on a bridge you don't fully trust.

## Acknowledgements

Modeled on the [GitHub Security Lab's guide to writing a
SECURITY.md](https://docs.github.com/en/code-security/getting-started/adding-a-security-policy-to-your-repository).
