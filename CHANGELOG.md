# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- Removing an account no longer leaves rules that named it, which recreated it as an empty slot on the next change.
- Renaming an account carries its rules with it, so launches stop routing to the old directory.
- A rule change no longer overwrites a session's newer credential with the account's spent one.
- A poke now shows its started windows within seconds instead of after the next poll.

### Changed

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
