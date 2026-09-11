# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `ccm menubar install` and `ccm menubar uninstall` to start the menu bar app at login
  without editing a plist by hand.
- A fake-data demo for the CLI, menu bar and README screenshots in a throwaway home.
- Automatic start of weekly usage windows, off by default: a menu bar toggle and `ccm auto-start on`.
- Spend a Codex reset credit from the account's menu or with `ccm reset <account>`, which puts
  every window of that account back to 0%.
- Codex sessions are routed by the same rules as Claude Code sessions: `ccm use <codex account>`
  sets one, and the `codex` wrapper from `ccm shell-init` picks the CODEX_HOME per terminal.
- Running Codex sessions are listed beside the Claude Code ones in `ccm sessions` and the menu
  bar, with the same columns, read from Codex's own lock files, state database and rollouts.
- The menu moves a Codex session the same way it moves a Claude Code one: a Codex row lists
  Codex accounts, says which rule chose the one it spends, and every profile and the default
  can name one Claude account and one Codex account at once.
- A Codex session that a rule has moved restarts in its own tab from the menu, in Terminal and
  iTerm2, and `ccm poke` and automatic start now cover Codex accounts.
- `ccm log` prints the recent credential events: refreshes, copies handed out and revoked
  logins, by fingerprint, from `~/.claude-manager/credentials.log`.

### Fixed

- While the menu bar app runs it is the only thing that refreshes a login: each session holds
  an access token and no refresh token, so no session can spend one. The app rotates half an
  hour ahead, before the Mac sleeps and when it wakes, and hands the new access token to every
  copy. It gives the refresh tokens back when it quits, and it keeps `~/.claude` current too.
- Credentials are refreshed on every 45 second pass and again when the Mac wakes, so a Mac
  that sleeps past a token's expiry no longer wakes with every copy of that login expired at
  once, which the server answered by revoking all of them.
- A session whose login the server revoked is marked `out` in the menu and in `ccm sessions`,
  and says it needs `/login` in its own tab. It used to be promised a switch within about
  thirty seconds, which it could not make: Claude Code stops reading the directory.
- Choosing an account for a project, profile or the default releases the older session rules
  under it, so every running session in that project moves together instead of the pinned
  ones staying behind. Session rules for terminals that no longer exist are dropped.
- Folder trust and the Claude in Chrome answers are shared across every session, so a new
  terminal no longer asks again for what you already answered somewhere else.
- Credentials refresh thirty minutes before expiry so Claude Code sessions receive the successor before rotating their shared token themselves.
- Moving a session to another account updates its recorded identity and drops stale
  cached usage, so `/status` names the account whose credential it holds.
- `ccm list` aligns the bars of model-scoped windows with the others, `ccm where` shortens
  your home to `~`, and a countdown over an hour reads `1h 47m` rather than `107m`.
- The hint after `ccm use` no longer says running sessions keep their account; they switch
  within about thirty seconds, and only a session without a directory of its own waits for a restart.
- The menu shows new sessions as soon as it opens and adds rows while it stays open.
  Usage and countdowns repaint in place; removed sessions disappear once it closes.
- Long account names no longer push the account picker columns out of alignment or wrap rows.
- The menu bar no longer sticks on "?" when the first session poll runs before the first refresh has loaded the accounts.

- Removing an account no longer leaves rules that named it, which recreated it as an empty slot on the next change.
- Renaming an account carries its rules with it, so launches stop routing to the old directory.
- A rule change no longer overwrites a session's newer credential with the account's spent one.
- A poke now shows its started windows within seconds instead of after the next poll.

### Changed

- CLI help now describes account usage and project routing.
- Renamed the project to claude-code-accounts. The `ccm` command, the config directories and existing rules are unchanged.

- The menu bar app reads each credential at most once every twenty seconds across its polls, instead of once per poll.
- The session poll no longer runs `ps` for every session on every pass.
- Launching `claude` through the shell wrapper imports less.

- A sign-in asks the profile endpoint once instead of twice.
- A rule change reads each account's credential once rather than once per running session.
- The credential sync reads each session copy once per pass.

## [0.1.0] - 2026-09-05

### Added

- Claude Code account tracking and routing by project, profile and default.
- Per-session account switching for running Claude Code sessions.
- `ccm poke` to start an idle account's usage window.
- A macOS menu bar app for usage and account rules.
- Codex account tracking with shared settings and history.
- Service marks in account chips to identify each provider.
- A single pinned account in the menu bar.
- `ccm --version` to print the installed version.

[Unreleased]: https://github.com/ryanhu22/claude-code-accounts/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ryanhu22/claude-code-accounts/releases/tag/v0.1.0
