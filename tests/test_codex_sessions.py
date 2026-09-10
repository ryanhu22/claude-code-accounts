"""Running Codex sessions, read from the lock files and state Codex writes."""

import datetime as _dt
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from claude_code_accounts import codex, codex_sessions, core, sessions
from fakes import session, sign_in

pytestmark = pytest.mark.usefixtures("fake_keychain", "fake_api", "no_git")

# The conftest replaces this so a real Codex on the machine cannot walk into a
# test's fake home. The tests that measure the reader itself want the original.
REAL_PROCESSES = codex_sessions.processes

MAIN, SUB, EXEC = "t-main", "t-sub", "t-exec"
STARTED = "2026-09-10T12:00:00Z"
CWD = "/Users/me/repo"


def epoch(stamp: str) -> float:
    return _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def locks_dir() -> Path:
    return Path(codex.DEFAULT_HOME, codex_sessions.LOCKS_DIR)


def database(home: str, rows: list[dict], version: int = 5) -> None:
    columns = codex_sessions.COLUMNS
    conn = sqlite3.connect(os.path.join(home, f"state_{version}.sqlite"))
    conn.execute(f"CREATE TABLE threads ({', '.join(columns)})")
    conn.executemany(f"INSERT INTO threads VALUES ({', '.join('?' * len(columns))})",
                     [tuple(row.get(c) for c in columns) for row in rows])
    conn.commit()
    conn.close()


def row(tid: str, source: str, thread_source: str, tokens: int, rollout: Path,
        title: str = "", name=None) -> dict:
    return {"id": tid, "rollout_path": str(rollout), "created_at": epoch(STARTED),
            "updated_at": epoch(STARTED) + 60, "source": source,
            "thread_source": thread_source, "cwd": CWD, "title": title, "name": name,
            # The database keeps the model the thread started on; the rollout's
            # last turn_context is the one it is answering with now.
            "tokens_used": tokens, "model": "gpt-5.6-sol", "git_branch": "main"}


def rollout(path: Path, busy: bool = True, half: bool = False) -> Path:
    """One thread's rollout, in the shape Codex appends it.

    The last token_count carries a null `info`, which is what Codex writes
    between turns, so a reader that trusts it reports no context at all.
    """
    records = [
        {"timestamp": STARTED, "type": "session_meta",
         "payload": {"id": path.stem, "cwd": CWD, "originator": "codex-tui",
                     "source": "cli", "thread_source": "user",
                     "context_window": 200_000, "timestamp": STARTED}},
        {"timestamp": STARTED, "type": "turn_context",
         "payload": {"model": "gpt-6-astra", "cwd": CWD}},
        {"timestamp": STARTED, "type": "event_msg", "payload": {"type": "task_complete"}},
        {"timestamp": STARTED, "type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"last_token_usage": {"input_tokens": 42_000},
                              "total_token_usage": {"total_tokens": 310_000},
                              "model_context_window": 272_000}}},
    ]
    if busy:
        records.append({"timestamp": STARTED, "type": "event_msg",
                        "payload": {"type": "task_started"}})
    records.append({"timestamp": STARTED, "type": "event_msg",
                    "payload": {"type": "token_count", "info": None}})
    text = "\n".join(json.dumps(r) for r in records)
    path.write_text(text + ('{"timestamp": "2026-09' if half else "\n"))
    return path


@pytest.fixture
def world(monkeypatch, tmp_path):
    """One exec process and one TUI process, each holding its own threads."""
    home = codex.DEFAULT_HOME
    locks_dir().mkdir(parents=True)
    rollouts = tmp_path / "rollouts"
    rollouts.mkdir()
    for tid in (MAIN, SUB, EXEC):
        locks_dir().joinpath(tid + ".lock").write_text("")
        rollout(rollouts / (tid + ".jsonl"), busy=tid != EXEC)
    database(home, [
        row(MAIN, "cli", "user", 5_000, rollouts / (MAIN + ".jsonl"),
            title="Make the sessions list show Codex\nand the rest of the message"),
        row(SUB, '{"subagent":"review"}', "subagent", 2_500, rollouts / (SUB + ".jsonl")),
        row(EXEC, "exec", "user", 700, rollouts / (EXEC + ".jsonl"),
            title="run the suite", name="nightly"),
    ])
    # An older schema left behind by a previous Codex. The highest number wins,
    # so nothing here may reach the session list.
    database(home, [row(MAIN, "cli", "user", 99, rollouts / (MAIN + ".jsonl"),
                        title="stale")], version=4)
    held = {101: (CWD, [EXEC]), 202: (CWD, [MAIN, SUB])}

    def open_files(pid, proc_start=""):
        cwd, ids = held.get(pid, ("", []))
        return cwd, [i for i in ids if locks_dir().joinpath(i + ".lock").exists()]

    monkeypatch.setattr(codex_sessions, "processes", lambda: [
        (101, "Wed Sep 10 12:00:01 2026", "codex exec run the suite"),
        (202, "Wed Sep 10 11:00:02 2026", "codex")])
    monkeypatch.setattr(codex_sessions, "open_files", open_files)
    monkeypatch.setattr(sessions, "_environ", lambda pid, start="": (
        {"CODEX_HOME": home, "TERM_SESSION_ID": f"T{pid}",
         "TERM_PROGRAM": "iTerm.app"}, "ttys004"))
    return rollouts


def test_live_reads_a_session_per_process(world):
    live = codex_sessions.live()
    assert [s.pid for s in live] == [101, 202]
    assert all(s.provider == "codex" and s.is_codex for s in live)
    by_pid = {s.pid: s for s in live}
    exec_session, tui = by_pid[101], by_pid[202]
    assert (exec_session.kind, tui.kind) == ("bg", "interactive")
    assert (exec_session.status, tui.status) == ("idle", "busy")
    assert (tui.session_id, tui.cwd, tui.term_id) == (MAIN, CWD, "T202")
    assert tui.term_program == "iTerm.app" and tui.tty == "ttys004"
    assert tui.env_config_dir == codex.DEFAULT_HOME == tui.config_dir
    assert tui.started_at == epoch(STARTED)
    # The null info on the last token_count says nothing, so the one before it
    # is still what the thread's context reads.
    assert (tui.context_tokens, tui.context_window) == (42_000, 272_000)
    assert tui.window == 272_000 and round(tui.context_pct) == 15
    assert tui.model == "gpt-6-astra"
    # The title is one line and no longer than a row can hold, and a thread
    # with a name of its own says so.
    assert tui.title == "Make the sessions list show Codex"
    assert (tui.name, tui.name_source) == ("", "derived")
    assert (exec_session.name, exec_session.name_source) == ("nightly", "user")
    # The subagent thread spends on this session's behalf, and the rollout is
    # ahead of the database for the main thread: 310000 + 2500.
    assert tui.spent.total == 312_500
    assert exec_session.spent.total == 310_000


def test_a_process_holding_no_lock_is_not_a_session(world):
    locks_dir().joinpath(EXEC + ".lock").unlink()
    assert [s.pid for s in codex_sessions.live()] == [202]


def test_live_answers_when_a_rollout_is_half_written(world):
    path = rollout(world / (MAIN + ".jsonl"), half=True)
    g = codex_sessions.digest(str(path))
    assert g.busy and g.context_tokens == 42_000 and g.model == "gpt-6-astra"
    assert g.started_at == epoch(STARTED)


def test_a_long_turn_is_still_read_as_busy(world, monkeypatch):
    """A turn writes its tool output after `task_started`, and can bury it."""
    path = world / (MAIN + ".jsonl")
    filler = {"timestamp": STARTED, "type": "response_item",
              "payload": {"type": "message", "text": "x" * 400}}
    with path.open("a") as f:
        for _ in range(20):
            f.write(json.dumps(filler) + "\n")
    monkeypatch.setattr(codex_sessions, "TAIL_BYTES", 1_024)
    assert codex_sessions.digest(str(path)).busy


def test_threads_without_a_readable_database(world, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert codex_sessions.threads(str(empty), [MAIN]) == {}
    Path(empty, "state_5.sqlite").write_text("not a database")
    assert codex_sessions.threads(str(empty), [MAIN]) == {}
    assert codex_sessions.threads(codex.DEFAULT_HOME, []) == {}
    # The real home still answers, and from the newest schema it holds.
    assert codex_sessions.threads(codex.DEFAULT_HOME, [MAIN])[MAIN]["tokens_used"] == 5_000


def test_processes_keeps_sessions_and_drops_the_other_subcommands(monkeypatch):
    lines = [
        "  101 Wed Sep 10 12:00:01 2026 codex exec run the suite",
        "  202 Wed Sep 10 11:00:02 2026 /opt/homebrew/bin/codex",
        "  303 Wed Sep 10 10:00:03 2026 node /x/node_modules/@openai/codex/bin/codex.js resume",
        "  404 Wed Sep 10 09:00:04 2026 codex login --browser Safari",
        "  505 Wed Sep 10 08:00:05 2026 claude --resume",
    ]
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "\n".join(lines) + "\n")

    monkeypatch.setattr(codex_sessions.subprocess, "run", run)
    found = REAL_PROCESSES()
    assert [pid for pid, _, _ in found] == [101, 202, 303]
    assert found[0] == (101, "Wed Sep 10 12:00:01 2026", "codex exec run the suite")
    assert calls == [["ps", "-axo", "pid=,lstart=,command="]]


def test_open_files_reads_the_cwd_and_the_locks_it_holds(monkeypatch):
    locks = "/Users/me/.codex/" + codex_sessions.LOCKS_DIR
    out = "\n".join(["p202", "fcwd", "n" + CWD,
                     "f7", f"n{locks}/{MAIN}.lock",
                     "f8", "n/Users/me/.codex/config.toml",
                     "f9", "n/Users/me/.codex/other/stray.lock",
                     "f10", f"n{locks}/{SUB}.lock"]) + "\n"
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, out)

    monkeypatch.setattr(codex_sessions.subprocess, "run", run)
    assert codex_sessions.open_files(202, "start") == (CWD, [MAIN, SUB])
    # A lock outside the thread-writer-locks directory is somebody else's, and
    # the answer is remembered for the few seconds a tick is worth.
    assert codex_sessions.open_files(202, "start") == (CWD, [MAIN, SUB])
    assert calls == [["lsof", "-p", "202", "-F", "fn"]]


def test_dirs_to_accounts_names_a_codex_home(fake_keychain, fake_api):
    slot = Path(codex.ensure_account_dir("cx"))
    slot.joinpath("auth.json").write_text(json.dumps({"tokens": {"refresh_token": "cx"}}))
    home = codex.prepare_session("T202", "cx")
    claude = sign_in("a", "a@example.com", fake_api)
    accts = core.all_accounts(with_usage=False)
    owners = core.dirs_to_accounts([home, str(slot), claude], accts)
    assert owners[home] == "cx" and owners[str(slot)] == "cx"
    assert owners[claude] == "a"
    # Naming a Codex home costs no keychain call: its login is a file.
    fake_keychain.reset()
    assert core.dirs_to_accounts([home], []) == {home: "cx"}
    assert fake_keychain.reads == 0


def test_all_sessions_merges_both_tools_newest_first(world, fake_api, monkeypatch):
    sign_in("a", "a@example.com", fake_api)
    claude = session("term", CWD, "a")
    claude.updated_at = time.time() + 1_000
    monkeypatch.setattr(sessions, "live", lambda *args, **kwargs: [claude])
    live = core.all_sessions()
    assert [s.pid for s in live] == [claude.pid, 101, 202]
    assert [s.provider for s in live] == ["claude", "codex", "codex"]
