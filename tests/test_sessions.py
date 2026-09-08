"""Session scans reuse process metadata until the process changes."""

import subprocess

from claude_code_accounts import sessions


def test_environ_cache_uses_pid_and_start(monkeypatch):
    calls = []
    monkeypatch.setattr(sessions, "_ENV_CACHE", {})

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, (
            "PID TT STAT TIME COMMAND\n"
            "4242 s001 S 0:00 claude TERM_SESSION_ID=abc CLAUDE_CONFIG_DIR=/x\n"))

    monkeypatch.setattr(sessions.subprocess, "run", run)
    expected = ({"TERM_SESSION_ID": "abc", "CLAUDE_CONFIG_DIR": "/x"}, "ttys001")
    assert sessions._environ(4242, "start") == expected
    assert sessions._environ(4242, "start") == expected
    assert len(calls) == 1
    assert sessions._ENV_CACHE == {(4242, "start"): expected}
    assert sessions._environ(4242, "reused") == expected
    assert len(calls) == 2
