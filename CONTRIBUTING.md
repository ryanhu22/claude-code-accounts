# Contributing

Bug reports and small, focused pull requests are welcome. Include what you
expected, what happened, and the output of `ccm list`. Redact account names,
email addresses and project paths before posting. Include your macOS, Claude
Code and Codex CLI versions when relevant.

## Development setup

```sh
git clone https://github.com/ryanhu22/claude-code-manager
cd claude-code-manager
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
