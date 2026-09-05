# Security

## What this software touches

The tool reads and writes macOS login keychain items named
`Claude Code-credentials-*`. It can read the older `Claude Code-credentials`
item as a fallback.

Claude account slots live in `~/.claude-accts`. Per-session credential copies
live in keychain items tied to directories under `~/.claude-ctx`. Shared settings
and history link back to `~/.claude`. Rules, preferences and pending credential
writes live in `~/.claude-manager`.

Codex account slots live in `~/.codex-accts`. The default slot links to
`~/.codex`, including its `auth.json`. Additional slots hold their own
`auth.json` and link shared files back to `~/.codex`.

The tool calls the Claude usage endpoint at
`https://api.anthropic.com/api/oauth/usage` and the Codex usage endpoint at
`https://chatgpt.com/backend-api/wham/usage`. Sign-in, profile checks, token refresh
and `ccm poke` also send requests to the providers. These requests carry your
credentials. The tool does not send them to the maintainer.

## Report a vulnerability

Use GitHub private vulnerability reporting on the repository's
[Security tab](https://github.com/ryanhu22/claude-code-manager/security).
Do not post credentials or vulnerability details in a public issue.

Describe the affected version, the steps to reproduce, and the impact.
Expect an acknowledgement within a week. There is no bounty program.

## Supported versions

Security fixes target the latest release and `main`. Update to one of these
before reporting a problem with an older version.

## Out of scope

Report issues in Claude Code or the Codex CLI themselves to their maintainers.
Terms-of-service questions are covered in the README's
[Read this first](README.md#read-this-first) section.
