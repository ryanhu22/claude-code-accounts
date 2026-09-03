# claude-code-manager

Manage several Claude Code subscriptions on one Mac: see every account's usage,
give each project its own account, swap accounts without a browser, and start a
freshly reset account's window on purpose.

Ships a CLI (`ccm`) and a macOS menu bar app.

Account names accept short forms: any unique prefix or substring of a slot
name works, so `ccm swap rr` finds `account-c` and `ccm swap 200` finds
`account-a`.

### Start the menu bar app at login

```sh
cp packaging/com.claude-code-manager.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.claude-code-manager.plist
```

## Why

Claude Code stores one login per config directory. That makes several
subscriptions usable side by side, but nothing tells you how much of each is
left, which project is spending which one, or how to move a project from an
exhausted account to a fresh one. This does that.

## Model

Two kinds of directory, kept strictly separate:

- **Account slots** — `~/.claude-accts/<name>`, one per subscription. Each is
  signed in once and no session ever runs there, so its login stays valid and
  its usage is always readable.
- **Contexts** — the config dirs sessions actually run in, holding history and
  settings. A swap copies a slot's live login into a context.

Nothing here ever mints a credential. It copies whole login blobs Claude Code
itself wrote, so a swapped context keeps `subscriptionType`, scopes, and rate
limit tier, and behaves exactly like a normal login.

Identity is always confirmed with the API (`/api/oauth/profile`), never guessed
from a directory name or a cached file, because those drift.

## Install

```sh
uv tool install claude-code-manager           # CLI only
uv tool install "claude-code-manager[menubar]" # plus the menu bar app
```

## Use

```sh
ccm list                 # usage for every subscription
ccm where                # which context and account this directory uses
ccm swap <account>       # point this directory's context at another account
ccm poke <account>       # spend one token to start that account's 5h window
ccm projects             # projects with Claude Code activity in the last hour
ccm isolate              # give this project its own context
ccm unroute              # drop this project's routing override
ccm add <account>        # prints the sign-in command for a new slot
ccm-menubar              # the menu bar app
```

### Poking

A weekly reset leaves an account at 0% with no window running: the 5-hour
window only starts when you first use it. `ccm poke` starts it deliberately for
about 22 input tokens, so the window lines up with when you actually want it.

### Swaps and running sessions

A swap takes effect for **new** sessions. A session already running holds its
credentials in memory for its lifetime, so it keeps its old account until it
restarts (`ctrl+C` twice, then `claude -c`). Being idle does not help, and a
rate limit error is not an auth error, so it never re-reads.

Switching accounts also invalidates the prompt cache, which is keyed per
account and per model, so the first turn after a swap re-sends the
conversation. That is one time per swap, not per turn, and it is cheapest right
after `/clear` or a `/compact`.

## Notes

- macOS only for now. Credentials live in the login keychain, written through
  `security -i` so secrets stay out of the process argument list.
- Claude Code 2.1.224+ keys credentials per config dir as
  `Claude Code-credentials-<sha256(config dir)[:8]>`; the older bare item is
  read as a fallback.
- Routing (which project uses which context) is read from `~/.claude/subs.conf`:
  `path:<dir>=<context dir>` wins over named entries like `work=` / `default=`.

## Prior art

[claude-swap](https://github.com/realiti4/claude-swap) covers account switching
and usage with a larger feature set (auto-rotation, TUI, cross-platform). This
project starts from per-project routing instead: which account a repo uses is a
property of the repo, not of the terminal you happen to be in.

## License

MIT
