# claude-code-accounts

Track several Claude Code subscriptions, and optional Codex subscriptions, on
one Mac. See what is left on each and decide which Claude account pays for each
project or session. Use the CLI (`ccm`) or the macOS menu bar app.

Status: early, macOS only, used daily by its author. Expect rough edges.

[![CI](https://github.com/ryanhu22/claude-code-accounts/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ryanhu22/claude-code-accounts/actions/workflows/ci.yml)

<!-- screenshot goes here -->

## Read this first

This is an independent project. It is not affiliated with, endorsed by, or supported by Anthropic or OpenAI. Claude, Claude Code and the Claude mark are trademarks of Anthropic, PBC. OpenAI, ChatGPT, Codex and the OpenAI logo are trademarks of OpenAI. They appear in this tool only to say which service an account belongs to.

Know what you are running:

- This tool reads and moves your own sign-in credentials: the keychain items Claude Code writes and the `auth.json` file the Codex CLI writes. It calls endpoints those tools use internally. Those endpoints are not documented and can change or disappear without notice.
- Anthropic's terms for Claude Code say that OAuth sign-in is for Claude Code and Anthropic's own applications, that third-party developers may not collect, store or intermediate Claude.ai credentials or session tokens, and that requests may not be routed through Free, Pro or Max plan credentials. Three parts of this tool do that with your own accounts: `ccm login`, `ccm poke`, and the credential copying that lets a running session change account without a restart. Anthropic says it may enforce these restrictions without notice. Using those parts can put your Claude account at risk. Read the terms yourself: https://code.claude.com/docs/en/legal-and-compliance
- The Codex CLI is open source, and OpenAI maintainers have said that forks and tools like this are allowed under its terms. This tool reads the file the Codex CLI writes and signs in with the same flow the CLI uses. OpenAI's terms still apply to your account.
- If you want to stay well inside both sets of rules, use the routing and session features only, sign in through Claude Code itself (`ccm add <name>` prints the command), and do not use `ccm login` or `ccm poke` for Claude accounts.

You use this software at your own risk. See the MIT license.

## Why

Claude Code keeps one login per config directory, so several subscriptions can
coexist. Nothing tells you how much of each is left, which project is spending
which one, or how to move a project off an exhausted account. This does.

## Requirements

- macOS 13 or newer for PyObjC.
- Python 3.10 or newer.
- `uv` is recommended for installation.
- Claude Code 2.1.224 or newer for per-config-dir keychain items.
- The Codex CLI, only if you track Codex accounts.

## Install

Installs come from GitHub. There is no PyPI release yet.

```sh
uv tool install "claude-code-accounts[menubar] @ git+https://github.com/ryanhu22/claude-code-accounts"
```

For the CLI alone:

```sh
uv tool install "claude-code-accounts @ git+https://github.com/ryanhu22/claude-code-accounts"
```

Sign each subscription in once, through the browser:

```sh
ccm login work                          # or --browser "Google Chrome"
```

The browser returns the code to a local port, so there is nothing to copy. The
page is asked to land on the account that slot last held, because switching
account part way through is what loses the code. `--paste` falls back to typing
it in, and so does the app if nothing can listen locally.

The menu bar does the same thing: an account that is signed out offers
**Sign in**, and lets you pick the browser. Which browser matters, because the
sign-in uses whichever account that browser is already logged into. With
several subscriptions that is exactly how the wrong one gets attached to a
name, so a sign-in that lands on a different account than the slot held before
says so.

Then let the shell pick the account for you. Add to `.zshrc`:

```sh
eval "$(ccm shell-init)"
```

That defines a `claude` wrapper which resolves the right config directory
before launching. To start the menu bar app at login, get the LaunchAgent
from a checkout of this repository:

```sh
git clone https://github.com/ryanhu22/claude-code-accounts
cd claude-code-accounts
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp packaging/com.claude-code-accounts.plist "$HOME/Library/LaunchAgents/"
sed -i '' "s|\$HOME|$HOME|g" "$HOME/Library/LaunchAgents/com.claude-code-accounts.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.claude-code-accounts.plist"
```

launchd does not expand `$HOME` in `ProgramArguments` or the other plist values.
The `sed` command replaces it with your home path in the installed copy.
The plist expects `ccm-menubar` in `~/.local/bin`; adjust that path if your
`uv` tool directory is elsewhere.

## Which account a session uses

Four rules, most specific first:

| scope | reaches | set with |
|---|---|---|
| session | one terminal | `ccm use <account> --session` |
| project | one repository and its worktrees | `ccm use <account>` |
| profile | every repository in a named group | `ccm use <account> --profile work` |
| default | everything no rule covers | `ccm use <account> --default` |

A **profile** is a named list of repositories that share a subscription, so
"put all my work repos on this account" is one rule and not one per repo.

```sh
ccm profiles                      # every rule, least specific first
ccm profile new work
ccm profile add work              # put this repository in it
ccm use acme --profile work       # move the whole group
ccm where                         # what this directory resolves to, and why
```

Account names take any unique prefix or substring, so `ccm use wo` finds
`work`.

Everything is in the menu bar too. Each session row offers the same three
scopes, and a PROFILES section shows which account each group uses.

## Changes reach running sessions

A session re-reads its keychain item about every thirty seconds, so a rule
change lands without a restart. Measured, by pointing a live session's
directory at a credential that could only fail:

```
msg1 (real credential)   is_error=False  'one'
credential replaced with garbage tokens
msg@35s                  is_error=True   'Failed to authenticate: ...'
```

That is what per-session config directories are for. Each session gets its own
directory, so writing a credential into one moves that session and nothing
else. Sessions started before they had one are the exception: there is nowhere
to write that only they would see, so those still need a restart, and their row
says so and offers to bring that terminal tab to the front.

Switching accounts invalidates the prompt cache, which is keyed per account and
per model, so the first turn after a move re-sends the conversation. Once per
move, not per turn, and cheapest right after `/clear` or `/compact`.

### Keeping the copies alive

Several directories holding one account means several copies of one refresh
token, and a refresh token is single use: whichever session refreshes first
spends it for the rest. Racing Claude Code for it is not winnable, so the app
does not try. Every 45 seconds it takes the newest credential of each lineage,
whoever produced it, and hands it to the copies that are behind. A session left
holding a spent token recovers on its own, because of that same thirty-second
re-read.

The account's own directory is authoritative. A session copy is promoted over
it only when it is genuinely newer *and* the API confirms it belongs to that
same account: a directory that has not caught up with a rule change is holding
a different login, and copying that around would mix two accounts together.
Writing a credential into a directory also drops the cached idea of whose
directory it is, since that answer decides what gets copied where.

## The other commands

```sh
ccm list                  # usage for every subscription
ccm sessions              # every running session and what pays for it
ccm poke <account>        # spend one token to start that account's 5h window
ccm unpin                 # drop this terminal's rule
```

### Poking

A weekly reset leaves an account at 0% with no window running: the 5-hour
window starts when you first use it. `ccm poke` starts it deliberately for
about 22 input tokens, so the window lines up with when you want it.

## Codex accounts

`ccm list` also tracks OpenAI Codex subscriptions: their plan, usage windows,
credits and reset credits. Sign another account in through the browser:

```sh
ccm login <name> --codex
```

The login already in `~/.codex` appears as `codex`, through a symlink rather
than a second copy of its refresh token. Each additional account owns a
`CODEX_HOME` at `~/.codex-accts/<name>` with only `auth.json` of its own;
everything else links back to `~/.codex`, so settings and history stay shared.

Windows differ by plan. Pro Lite has a weekly window only; plans with a
five-hour window show that too. Routing Codex accounts is not supported yet.

## How it works

### One config directory per account

Each account owns `~/.claude-accts/<name>`, and that is where sessions run.
Settings, commands and `projects/` symlink back to `~/.claude`, so every
account shares one set of them and `claude -c` finds the same history whichever
subscription is paying.

This is what makes a rule change free. A rule names an account, so pointing a
project elsewhere rewrites one line: no credential is copied, nothing can be
half-written, and there is never a second copy of a login to fall out of sync.

Per-project settings do follow the project. `.claude.json` keeps trust, allowed
tools and MCP servers under the project's path, so that entry is copied across
and a move does not re-ask for trust.

Rules live in `~/.claude-manager/config.json`. Claude Code only understands
`CLAUDE_CONFIG_DIR` and the shell must resolve a directory before it can launch
anything, so they are also flattened into a grep-able `routes.conf` on every
change. If this tool is broken or missing, the last table still works.

### A credential is never invented

Signing in is an OAuth PKCE flow against `platform.claude.com`, asking for the
same scopes a real Claude Code login carries. Every field of the stored
credential comes from the token response or from `/api/oauth/profile`, and the
result is checked against the live API before it is written.

A blob missing `subscriptionType` or `rateLimitTier` still authenticates, but
Claude Code then opens the session as "API Usage Billing" instead of the plan
the user pays for. So a sign-in that cannot read back its own identity and plan
is refused rather than saved: a login that half works is harder to diagnose
than one that never happened.

### Credential writes take Claude Code's locks

Claude Code guards its own token refresh with `proper-lockfile` directory locks
(`<config dir>/.oauth_refresh.lock`, then the legacy sibling `<config dir>.lock`).
A refresh that lands inside its window rotates the token twice, and whichever
copy keeps the spent one fails its next refresh and looks signed out. So every
credential write here holds the same locks and re-reads under them, and a
rotated token is pushed to any other copy of the same generation. The protocol
was documented by [claude-swap](https://github.com/realiti4/claude-swap),
verified against the Claude Code 2.1.218 bundle.

### A failed request is never a broken login

Only the server refusing a token says anything about a login: a 401 or 403 on
the profile endpoint, or an RFC 6749 `invalid_grant` on refresh. A rate limit or
a network failure falls back to the last known answer.

That matters because these endpoints are rate limited per account, and running
sessions poll them too, so the busiest account is the one whose requests fail.
Identity is cached against the credential's fingerprint (an account cannot
change while its credential does not) and carried across rotations, so the
steady state asks nothing. Usage is cached with a per-account backoff, and a
429 serves the last payload rather than blanking the row; reset times in it are
absolute, so a cached row still counts down.

### What a session row says

Sessions come from Claude Code's own registry, `<config dir>/sessions/<pid>.json`,
which carries the pid, cwd, status and the name it shows for the session. That
also catches config dirs reached through a path alias: Claude Code keys the
keychain on the path *string*, so a symlink and its target are two logins.

Claude Code names a session `<repo>-<hash>` until it has something better, so a
row prefers the branch for a worktree (which is also *which* worktree), a name
that was chosen deliberately, then the title Claude Code wrote for the
conversation.

The `ctx` bar is how full the session's context window is, from the last
request it made. Beside it is what the session has spent over its whole life,
counted incrementally: a transcript is append-only, so each pass resumes from
the byte the last one stopped at and the offset is kept on disk. Ten sessions
over 200MB of transcripts cost 0.36s cold and nothing after.

The submenu breaks the total into four figures rather than letting one number
speak for the session. A cache read costs a fraction of a fresh input token,
and a long conversation re-reads its whole context every turn, so cache reads
dominate the total.

## Notes

- macOS only. Credentials stay in the login keychain, written through
  `security -i` so secrets never reach the process argument list.
- Claude Code 2.1.224+ keys credentials per config dir as
  `Claude Code-credentials-<sha256(config dir)[:8]>`; the older bare item is
  read as a fallback.
- Nothing here mints a credential. The only ones that exist are what Claude
  Code wrote at login, which is why a session started this way keeps its real
  `subscriptionType`, scopes and rate-limit tier.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests and pull requests.

## Security

Report vulnerabilities privately. See [SECURITY.md](SECURITY.md).

## Prior art

[claude-swap](https://github.com/realiti4/claude-swap) covers account switching
and usage with a larger feature set (auto-rotation, TUI, cross-platform), and
documented the lock protocol used here. This project starts from a different
question: which account pays for a piece of work is a property of the work, not
of the terminal you happen to be sitting in.

## License

MIT
