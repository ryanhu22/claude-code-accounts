# How it works

Design notes for claude-code-accounts: where the config directories live, how
several copies of one login stay valid, and why a failed request never counts
as a broken login. The [README](../README.md) has the short version.

## One config directory per account

Each account owns `~/.claude-accts/<name>`, and that is where sessions run.
Settings, commands and `projects/` symlink back to `~/.claude`, so every
account shares one set of them and `claude -c` finds the same history
whichever subscription is paying.

This is what makes a rule change cheap. A rule names an account, so pointing
a project elsewhere rewrites one line. No credential is copied, nothing can be
half-written, and there is never a second copy of a login to fall out of sync.

Per-project settings follow the project. `.claude.json` keeps trust, allowed
tools and MCP servers under the project's path, so that entry is copied across
and a move does not ask for trust again.

The answers Claude Code asks once follow the user instead. Folder trust and the
Claude in Chrome onboarding are shared across every directory, so answering in
one session answers for all of them, and a new session starts out trusting the
folders you already trusted. Turning the browser tools on or off in any session
sets them the same way everywhere.

Rules live in `~/.claude-manager/config.json`. Claude Code only understands
`CLAUDE_CONFIG_DIR`, and the shell must resolve a directory before it can
launch anything, so the rules are also flattened into a grep-able
`routes.conf` on every change. If this tool is broken or missing, the last
table still works.

Claude Code 2.1.224 and newer keys credentials per config directory as
`Claude Code-credentials-<sha256(config dir)[:8]>`. The older bare item is
read as a fallback. Credentials stay in the login keychain, written through
`security -i` so secrets never reach the process argument list.

## Keeping the copies alive

Several directories holding one account means several copies of one login, and
the refresh token inside a login is single use: whichever session spends it
first strands the rest. So while the menu bar app runs, it is the only thing
that refreshes an account. Every session directory holds an access token and
no refresh token, so no session can spend one. Claude Code accepts a login
like that and sends the access token it finds until the app replaces it.

The app rotates an account half an hour before its token expires, again when
the Mac is about to sleep, and again when the Mac wakes. Each new access token
goes straight to every copy of that account, and the 45 second pass picks up
whatever was busy at the time. A session left holding an old token catches up
on its own, because Claude Code re-reads its keychain item about every thirty
seconds.

Rotating before a sleep matters as much as rotating after a wake. No timer
runs while a Mac sleeps, so a Mac that slept past an expiry used to wake with
every copy of a login expired at once; two of them then refreshed with the
same spent token, and the server read that as reuse and revoked the whole
family. The app now goes to sleep on tokens with hours of life ahead of them,
and it looks again on wake, and 5, 10 and 20 seconds after that.

Quit the app, or stop it with `launchctl`, and it writes the refresh tokens
back into every session directory on the way out. The sessions then refresh on
their own, the way they did before the app existed, and two sessions on one
account can collide again. The app is what makes several sessions per account
safe. `ccm resolve` hands out a whole login too, because a machine with no app
running has nothing else to refresh for it; the app takes the refresh token
off that copy on its next pass.

`~/.claude` is a copy as well, for anything that runs `claude` without the
shell wrapper. The app keeps it on the login of the account it is signed in
as. That one keeps its refresh token, so a bare `claude` can still refresh
while the app is not running.

A session whose login the server revoked says `out` in the status column, in
red. Claude Code clears the dead token from that directory and then stops
reading it, so the working login the app writes there afterwards never
arrives. Run `/login` in that tab, or press ctrl+C twice and run `claude -c`.
Nothing else in the app can reach it.

`ccm log` prints what has happened to the credentials: every refresh, every
copy handed out, and every login the server revoked, with a fingerprint of
each generation and no tokens. The file is `~/.claude-manager/credentials.log`.

The account's own directory is authoritative. A session copy is promoted over
it only when it is newer *and* the API confirms it belongs to the same
account. A directory that has not caught up with a rule change is holding a
different login, and copying that around would mix two accounts together.
Every write re-checks under Claude Code's own locks, so a rotation that lands
between a read and a write is never overwritten.

## A credential is never invented

Signing in is an OAuth PKCE flow with its sign-in page on `claude.com`, the same
page Claude Code's "Claude account with subscription" option opens. The token
endpoint stays on `platform.claude.com`, and the flow asks for the same scopes
a real Claude Code login carries. Every field of the stored credential comes
from the token response or from `/api/oauth/profile`, and the result is
checked against the live API before it is written.

A credential missing `subscriptionType` or `rateLimitTier` still
authenticates, but Claude Code then opens the session as "API Usage Billing"
instead of the plan you pay for. So a sign-in that cannot read back its own
identity and plan is refused rather than saved. A login that half works is
harder to diagnose than one that never happened.

## Credential writes take Claude Code's locks

Claude Code guards its own token refresh with `proper-lockfile` directory
locks: `<config dir>/.oauth_refresh.lock`, then the legacy sibling
`<config dir>.lock`. A refresh that lands inside its window rotates the token
twice, and whichever copy keeps the spent one fails its next refresh and looks
signed out. So every credential write here holds the same locks and re-reads
under them, and a rotated token is pushed to any other copy of the same
generation. The protocol was documented by
[claude-swap](https://github.com/realiti4/claude-swap), verified against the
Claude Code 2.1.218 bundle.

## A failed request is never a broken login

Only the server refusing a token says anything about a login: a 401 or 403 on
the profile endpoint, or an RFC 6749 `invalid_grant` on refresh. A rate limit
or a network failure falls back to the last known answer.

That matters because these endpoints are rate limited per account, and running
sessions poll them too, so the busiest account is the one whose requests fail.
Identity is cached against the credential's fingerprint (an account cannot
change while its credential does not) and carried across rotations, so the
steady state asks nothing. Usage is cached with a per-account backoff, and a
429 serves the last payload rather than blanking the row. Reset times in it
are absolute, so a cached row still counts down.

## What a session row says

Sessions come from Claude Code's own registry, `<config dir>/sessions/<pid>.json`,
which carries the pid, cwd, status and the name it shows for the session. That
also catches config directories reached through a path alias: Claude Code keys
the keychain on the path *string*, so a symlink and its target are two logins.

Claude Code names a session `<repo>-<hash>` until it has something better, so
a row prefers the branch for a worktree (which is also *which* worktree), then
a name that was chosen deliberately, then the title Claude Code wrote for the
conversation.

The `ctx` bar is how full the session's context window is, from the last
request it made. Beside it is what the session has spent over its whole life,
counted incrementally: a transcript is append-only, so each pass resumes from
the byte the last one stopped at, and the offset is kept on disk. Ten sessions
over 200MB of transcripts cost 0.36s cold and nothing after. The submenu
breaks the total into four figures, because a cache read costs a fraction of a
fresh input token and dominates a long conversation.

Codex keeps no session registry, so each Codex row is read from what Codex
writes itself. The lock file a process holds says which threads are live, its
state database names the thread, and the thread's rollout gives the model, the
context and the turn that is running now.

## What it costs to run

Every keychain read is one `security` subprocess, about 15 to 20 ms, and that
is the unit cost that matters. The app reads each credential at most once
every twenty seconds across its polls, reads every credential fresh before it
writes one, and asks the process table once per session rather than once per
poll. `scripts/bench.py` reports the wall time and the keychain calls of every
move, and the test suite pins the exact call count of each so a regression
fails CI.
