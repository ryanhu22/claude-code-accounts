# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `ccm menubar install` and `ccm menubar uninstall` to start the menu bar app at login
  without editing a plist by hand.
- A fake-data demo for the CLI, menu bar and README screenshots in a throwaway home.
- Automatic start of weekly usage windows, off by default: a menu bar toggle and `ccm auto-start on`.

### Fixed

- `ccm list` aligns the bars of model-scoped windows with the others, `ccm where` shortens
  your home to `~`, and a countdown over an hour reads `1h 47m` rather than `107m`.
- The hint after `ccm use` no longer says running sessions keep their account; they switch
  within about thirty seconds, and only a session without a directory of its own waits for a restart.
- The menu now updates while it is open. Usage, countdowns and session rows repaint in place; rows are added or removed once it closes.
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
