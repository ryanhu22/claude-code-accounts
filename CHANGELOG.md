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

### Fixed

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
