# End-to-end tests

These run the real `ccm` command as a subprocess, through whole user flows,
against a fake Anthropic and OpenAI server on a loopback port. Nothing in
them can reach a real account: not the keychain, not `~/.claude*`, not
`~/.codex*`, not launchd, not any real host.

```sh
uv run pytest tests/e2e          # or: uv run pytest -m e2e
uv run pytest -m "not e2e"       # the unit suite alone
```

Every test under this directory gets the `e2e` marker from `conftest.py`.
They run as part of the normal suite and take under a second each.

## What the sandbox is

`harness.Sandbox` builds, in a temp directory:

- `home/`: HOME for the subprocess. `~/.claude`, `~/.claude-accts`,
  `~/.claude-manager`, `~/.claude-ctx`, `~/.codex`, `~/.codex-accts` all land
  here.
- `bin/`: first on PATH. A fake `security` backed by `keychain.json`
  (`stubs/security.py`), stubs for `claude`, `codex`, `open`, `ps`,
  `lsof`, `pmset`, `osascript` and `launchctl` (`stubs/tools.py`), and
  `ccm` itself, so the generated shell resolver finds it by name. The
  rest of PATH is `/usr/bin:/bin`.
- An environment (`sandbox.env`) with HOME, PATH, USER=e2e, a fixed
  TERM_SESSION_ID, the CCM_* URL overrides for the fake server, and
  `https_proxy`/`http_proxy` set to a closed loopback port with `no_proxy`
  covering loopback. A request for a real host therefore dies with
  "connection refused" even if an override were missing.

The fake `security` refuses to run unless `CCM_E2E_KEYCHAIN_FILE` is set, so
it can never fall through to the real keychain by accident. Every call it
gets is logged to `keychain.json.log` with secrets redacted.
`test_e2e_isolation.py` holds the tripwires.

The pytest process itself is put into the sandbox too (`isolated_home` in
`conftest.py` replaces the unit suite's fixture of the same name): HOME and
the module constants point at the sandbox and the fake server, subprocesses
are allowed, and a socket may connect to loopback only. That is what lets
menu bar code run in-process against the same sandbox.

## Fixtures

| fixture | what you get |
|---|---|
| `fake_server` | a started `FakeServer`; closed after the test |
| `sandbox` | a `Sandbox` on `tmp_path`, wired to `fake_server` |
| `run_ccm` | `sandbox.run`: `run_ccm("list")` returns the `CompletedProcess` of the real `ccm` entry point (the one next to the test interpreter), with `stdout` and `stderr` as text |

`Sandbox` methods worth knowing:

- `run(*args, input=None, timeout=60, cwd=None, env=None)`, `popen(*args)`
  and `python(code, env=None)` (a snippet with the package importable, in
  the sandbox environment).
- `seed_claude(name, email, **account_fields)`: a signed-in Claude account
  without the browser. Writes the slot dir, its `.claude.json` identity and
  the keychain item. Returns the credential blob.
- `seed_codex(name, email, **account_fields)`: a signed-in Codex account, by
  writing its `auth.json`.
- `sign_in(name, browser="", paste=False, timeout=30)`: the real
  `ccm login <name>` flow. Starts ccm, waits for the URL it asks `open` for,
  presses Authorize on the fake page, follows the redirect to ccm's loopback
  callback, and returns the `CompletedProcess`. With `paste=True` it feeds
  the hosted `code#state` on stdin instead.
- `sign_in_codex(name)`: the same for `ccm login <name> --codex`. Binds port
  1455, as Codex does; skip when it is busy (see the smoke test). A refusal
  because another sign-in holds the port for a moment is retried for up to
  the timeout, since tests beside this one may use the port too.
- `seed_session(config_dir, cwd, term_id=TERM_ID, name="", status="idle")`:
  a Claude Code session running right now, for `ccm sessions`. Writes the
  registry file Claude Code keeps under `<config dir>/sessions/`, starts a
  `sleep` to own its pid, and tells the fake `ps` (via `ps.json`,
  `register_process`) what environment that pid has. `close()` kills the
  sleeps; the fixture does that for you.
- `blob(config_dir)`: the credential a config dir holds, from the fake
  keychain. `keychain()`, `keychain_log()`.
- `opened_urls()`, `tool_calls(tool)`, `codex_execs()`, `tripwire()`.
- `harness.plain(text)` strips the colour codes from an output.
- `slot(name)`, `codex_slot(name)`, `default_config`.
- `apply_in_process(setenv, setattr)`: what the autouse fixture calls.

`FakeServer` (`fake_server.py`):

- `add_claude(email, tier=..., limits=[...], grants=[...])` returns a mutable
  `ClaudeAccount`; change `limits` or `grants` between calls and the next
  request sees it. `add_codex(email, plan=..., usage={...}, credits=[...])`
  likewise.
- `browser` / `codex_browser`: the account the "browser" is signed in as.
  Unset, the sign-in page uses the `login_hint` ccm sent, else the only
  account. Set it to another email to test a sign-in on the wrong account.
- `script(prefix, status, body=None, headers=None, method=None, times=1)`:
  the next `times` requests whose path starts with `prefix` get this reply.
  `script("/api/oauth/usage", 429, {...}, headers={"Retry-After": "300"})`.
- `requests` and `calls(prefix, method)`: every request, with `.query`,
  `.headers`, `.json`, `.form` and `.token`.
- `issue(email)` / `blob(email)` / `codex_auth(email)`: fresh tokens.
  `revoke(email)`: every token of that account stops working.
- `pokes`, `resets`, `authorizations`: what the flows did.
- `deny_next_authorize = True`: the next Authorize press comes back as
  `error=access_denied`.

Token behaviour mirrors the real servers: codes are single use and PKCE is
checked; a refresh token is single use and a reused one gets `400
invalid_grant`; the token response carries `refresh_token_expires_in`
(`refresh_lifetime`: a number, a string, or None to leave it out). Every
refresh grant is counted in `refresh_uses`, and `reused_refresh_tokens`
lists the ones sent twice; the `fleet` fixture fails a test that caused one
unless it set `allow_reuse`. `lost_refresh_replies = N` makes the next N
refresh grants spend their token and drop the connection without a reply.
`generation(access_token)` says which grant of its account a token is.

## Sessions that are real processes

`lifecycle.py` (fixture `fleet`) starts a Claude Code session the way the
shell wrapper does: the real `ccm resolve` prepares the terminal's dir, a
live `sleep` process stands in for Claude Code with the registry file it
would write, and the `ps` stub answers for that pid from the sandbox's
`ps.json`, so `core.credential_dirs()` and `sessions.live()` find it as the
app does. `claude_code_refresh(url, dir)` does what a session does with a
refresh token of its own, under Claude Code's locks, with the re-read that
skips a fresh token. `menubar_app(live)` gives the app's sleep, wake and
tick handlers on an app that never drew a menu, and `check_invariants`
asserts the three things every scenario must keep: no session copy with a
refresh token while the app runs, no token sent twice, no copy behind its
slot. A file named `<keychain>.fail-writes` makes the fake `security`
refuse every write; `pmset.dark` in the sandbox makes `pmset` report a
dark wake.

## Endpoints the fake serves

Claude: `GET /cai/oauth/authorize` (the sign-in page), `GET /cai/oauth/approve`
(the button), `POST /v1/oauth/token`, `GET /api/oauth/profile`,
`GET /api/oauth/usage` (with the `cedar_ember` block when asked for it),
`POST /api/organizations/{org}/reset_rate_limits`, `POST /v1/messages`
(the poke; it starts every window that has no clock).

Codex: `GET /oauth/authorize`, `GET /oauth/approve`, `POST /oauth/token`
(form or JSON), `GET /backend-api/wham/usage`,
`GET /backend-api/wham/rate-limit-reset-credits`, and `POST .../consume`.

## The URL overrides

Read once at import by `core.py`, `oauth.py` and `codex.py`. Unset, every
URL is the production string.

| variable | replaces |
|---|---|
| `CCM_API_BASE` | `https://api.anthropic.com` |
| `CCM_TOKEN_URL` | both token URLs (the tuple becomes this one URL) |
| `CCM_AUTHORIZE_URL` | `https://claude.com/cai/oauth/authorize` |
| `CCM_CODEX_AUTH_BASE` | `https://auth.openai.com` (authorize and token) |
| `CCM_CODEX_API_BASE` | `https://chatgpt.com` (usage and reset credits) |

`FakeServer.env()` returns all five for a running server. The Codex
callback port stays 1455: that is what the Codex client id is registered
for, and ccm sends it as the redirect URI.

## A browser test

`ccm login` listens on `http://localhost:<port>/callback`
(`oauth.Callback`, bound to 127.0.0.1 on a port the kernel picks) and asks
the default browser to open the authorize URL. The fake sign-in page is an
ordinary page with one button, `#authorize`, so a real browser can press
it and land on ccm's own "Signed in." page.

`serve_signin.py` sets all of that up for a test that drives a browser:

```sh
uv run python tests/e2e/serve_signin.py --account work --email work@example.com
```

It prints one JSON line as soon as ccm has asked for a browser:

```json
{"authorize_url": "http://127.0.0.1:PORT/cai/oauth/authorize?...",
 "fake_server": "http://127.0.0.1:PORT",
 "callback_url": "http://localhost:PORT2/callback",
 "sandbox": "/var/folders/.../ccm-e2e-xxxx", "account": "work",
 "email": "work@example.com", "pid": 12345}
```

Open `authorize_url` in the browser, click `#authorize`, and the browser is
redirected to `callback_url` with the code. ccm exchanges the code before it
answers that request, so the page is the outcome: "Signed in." with the
account (`oauth.done_page`), or "The sign-in did not finish." with the
reason (`oauth.failed_page`), when the token endpoint refused the code or
the sign-in page sent `access_denied`. A reload of that tab, even during
the exchange, shows the same page; a request with another state gets
`WRONG_SIGN_IN_PAGE` with a 400. When ccm exits, or `--timeout` seconds
(default 300) pass, the script prints a second line:

```json
{"done": true, "returncode": 0, "signed_in": true, "stdout": "...", "stderr": ""}
```

and exits with ccm's status. The sandbox is removed unless `--keep` is
passed. Nothing in it touches the real keychain or home directory, so it
is safe to run on a machine with live logins.

The failure modes are flags, and the same keywords on the `SignIn` class
the script is built on:

| flag | what happens |
|---|---|
| `--codex` | `ccm login --codex`, on port 1455 (`--plan` sets the fake plan) |
| `--deny` | the Authorize press comes back as `access_denied` |
| `--browser EMAIL` | the browser is signed in as another account |
| `--seeded EMAIL` | the slot held that account before, so ccm says it changed hands |
| `--expired-code` | the token endpoint refuses the code as expired |
| `--slow-token SECONDS` | the exchange takes that long (reload the tab meanwhile) |
| `--busy-port` | something holds port 1455 before ccm starts; ccm exits 1 |

`test_e2e_signin_pages.py` runs these without a browser, so a regression in
the pages is caught by the ordinary test run.

## The browser suite

`e2e/` at the repository root holds a suite that renders all of this in
Chromium, with the [tester-army/e2e](https://github.com/tester-army/e2e)
runner (`@e2e-dev/web`, Playwright underneath). It is separate from pytest
because a few steps ask a model to judge the page:

```sh
npm --prefix e2e install                   # once
(cd e2e && npx e2e login openai)           # once: the model behind agent steps
npm --prefix e2e test                      # the whole suite
npm --prefix e2e test -- --grep Codex --headed
```

The runner starts `tests/e2e/signin_service.py` as the app under test (one
process for the run, stopped when the run ends) and each test asks it for a
sandboxed sign-in over HTTP, with the same keywords as the table above plus
`sandbox_of` to put a second account into the first one's home. Tests then
open `authorize_url` in the runner's browser, press Authorize, and read
ccm's page; `signins.outcome(id)` is how ccm ended, `signins.ccm(id,
"list")` runs another command in that sandbox. The six Codex tests skip
when port 1455 is busy, like the pytest ones.

What it covers: the Claude and Codex sign-ins end to end (page, exit
status, `ccm list`), a denied sign-in, a code the token endpoint refuses,
a forged state and a stray request against the callback, the callback
visited twice during the exchange and after ccm exited, a browser signed
in to another account, a second account, port 1455 busy, and the pages
themselves: no script, nothing fetched, the content in the HTML the server
sends, readable in dark and light mode, short plain sentences with no em
dashes or filler words.

Agent steps use the ChatGPT subscription signed in with `npx e2e login
openai` (model `gpt-6-luna`); three to four model calls per run, fewer once
the replay cache under `e2e/.e2e/` (ignored by git) has recorded the one
`agent.act`. Everything else is locators and `expect`.
