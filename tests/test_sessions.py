"""Session scans reuse process metadata until the process changes."""

import json
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


def _registry(tmp_path, cwd, name):
    config = tmp_path / "config"
    (config / "sessions").mkdir(parents=True)
    (config / "sessions/1.json").write_text(json.dumps({
        "pid": 1, "sessionId": "s1", "cwd": cwd, "name": name,
        "nameSource": "derived", "status": "idle", "kind": "interactive"}))
    return config


def test_a_worktree_row_takes_the_name_the_session_chose(monkeypatch, tmp_path):
    """The branch of a fresh worktree is its folder, which says nothing."""
    cwd = "/repos/acme/.claude/worktrees/agent-a05"
    config = _registry(tmp_path, cwd, "acme-9d")
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(json.dumps(
        {"type": "custom-title", "customTitle": "calendar sync"}) + "\n")
    monkeypatch.setattr(sessions, "alive", lambda pid: True)
    monkeypatch.setattr(sessions.transcripts, "find", lambda sid, roots: str(transcript))
    monkeypatch.setattr(sessions, "branch_of", lambda path: "worktree-agent-a0560e9575")
    live = sessions.live([str(config)], with_env=False, with_git=True, with_transcript=True)
    assert (live[0].name, live[0].name_source) == ("calendar sync", "user")
    assert live[0].detail == "calendar sync"


def test_a_worktree_row_keeps_its_branch_without_a_chosen_name(monkeypatch, tmp_path):
    cwd = "/repos/acme/.claude/worktrees/agent-a05"
    config = _registry(tmp_path, cwd, "acme-9d")
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(json.dumps(
        {"type": "ai-title", "aiTitle": "Fixing the calendar importer"}) + "\n")
    monkeypatch.setattr(sessions, "alive", lambda pid: True)
    monkeypatch.setattr(sessions.transcripts, "find", lambda sid, roots: str(transcript))
    monkeypatch.setattr(sessions, "branch_of", lambda path: "worktree-agent-a0560e9575")
    live = sessions.live([str(config)], with_env=False, with_git=True, with_transcript=True)
    assert live[0].name_source == "derived"
    assert live[0].detail == "worktree-agent-a0560e9575"
