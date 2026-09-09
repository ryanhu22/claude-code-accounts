# Contributing

Bug reports and small, focused pull requests are welcome. Include what you
expected, what happened, and the output of `ccm list`. Redact account names,
email addresses and project paths before posting. Include your macOS, Claude
Code and Codex CLI versions when relevant.

## Development setup

```sh
git clone https://github.com/ryanhu22/claude-code-accounts
cd claude-code-accounts
uv venv
uv pip install -e ".[menubar]" pytest ruff
uv run pytest
uv run ruff check src tests
```

Run the CLI from the checkout:

```sh
uv run ccm list
```

Run the menu bar app from the checkout:

```sh
uv run ccm-menubar
```

Run one menu bar instance at a time. Stop the installed instance before starting
one from the checkout, because two instances would both write the same caches.

## A throwaway home

Tests use temporary directories and fake credentials. They do not contact usage
endpoints or read the login keychain. Keep new tests that way.

For manual CLI work, redirect the home and manager directories before starting
Python so imported paths point at the throwaway tree:

```sh
ccm_test_home="$(mktemp -d)"
env HOME="$ccm_test_home" \
  CCM_ACCOUNTS_DIR="$ccm_test_home/.claude-accts" \
  CCM_HOME="$ccm_test_home/.claude-manager" \
  CCM_CODEX_ACCOUNTS_DIR="$ccm_test_home/.codex-accts" \
  CCM_SESSION_DIRS="$ccm_test_home/.claude-ctx" \
  uv run ccm shell-init
```

`CCM_ACCOUNTS_DIR` redirects Claude account slots and their usage, identity and
chip caches. `CCM_HOME` redirects rules, routes, the resolver, preferences and
pending credential writes. `CCM_CODEX_ACCOUNTS_DIR` redirects Codex account slots.
`CCM_SESSION_DIRS` redirects per-session Claude directories.

Those manager variables alone do not redirect `~/.claude`, `~/.codex` or the
transcript token cache. The throwaway `HOME` covers those paths. Clear inherited
`CLAUDE_CONFIG_DIR` and `CODEX_HOME` if you launch either CLI there.
Changing `HOME` does not isolate the macOS login keychain. Use the test suite
for credential work, or a separate macOS user for manual sign-in tests.

## Seeing the app without accounts

Run the demo with fake accounts, rules and running sessions in a throwaway home:

```sh
.venv/bin/python scripts/demo.py cli list
.venv/bin/python scripts/demo.py menubar
.venv/bin/python scripts/demo.py shots
```

The last command regenerates the README images into `docs/images/` from fake
data. It needs Google Chrome and Screen Recording permission for the terminal.
It opens a second menu bar item briefly. The demo uses its own home, so the
installed app can keep running. Use `--home PATH` before any CLI arguments to
choose an empty throwaway directory. The demo prints its home when it starts.

## Measuring latency

Run the fake bench with a throwaway home and no external tools or network:

```sh
.venv/bin/python scripts/bench.py
.venv/bin/python scripts/bench.py --accounts 5 --sessions 14 --runs 20 --security-ms 15
```

The table reports median wall time and keychain calls for each move. Each read
is one `security` subprocess, about 15 ms on this machine. Fake calls cost
almost nothing, so the estimate adds `(reads + writes) * security_ms` to the
wall time. Deletes are counted but excluded from that estimate.
`tests/test_moves.py` pins the exact keychain-call count of every move so a
regression fails CI.

To measure read-only operations against local accounts and running sessions:

```sh
.venv/bin/python scripts/bench.py --real
```

Real mode reads the login keychain and blocks network access. It disables
refresh, healing, adoption and cache writes. It times each `security` call and
prints their median. Its wall times already include keychain latency. Profile
counts are blocked lookup attempts, and session metadata caches stay warm.
Account names and credentials are never printed.

## Code conventions

This project targets macOS. Keep runtime code in the standard library, except
for the optional menu bar dependencies. Import AppKit inside functions so the
CLI and pure tests can load without it.

Keep the voice of the existing files. Comments explain why. Use short active
sentences with one idea per sentence. Do not use em dashes or emoji.

## Pull requests

Describe what changes for the user and why. Say how you tested it. Changes to
keychain access, `auth.json` or token refresh get extra scrutiny; explain which
credential paths your tests cover.

Use one imperative sentence as the commit title. Say what changed for the user.
Use the body to say why. Keep each pull request focused on one change.

Pull requests run CI and need it green. Run tests and Ruff before submitting.
Add an entry under Unreleased in `CHANGELOG.md` for user-visible changes.
