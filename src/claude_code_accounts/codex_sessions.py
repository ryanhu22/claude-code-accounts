"""Live Codex sessions, read from Codex's own lock files and state database.

Codex keeps no registry of running sessions the way Claude Code does, so the
process is what has to be asked. Every thread a Codex process writes is held
open under ``<CODEX_HOME>/thread-writer-locks/<thread id>.lock``, and the lock
file can outlive the process that made it, so the lock being HELD is what says
a thread is live. ``lsof`` on the process lists the locks it holds, which is
also what pairs a pid with its threads, and it reports the working directory in
the same call.

What each thread IS comes from Codex's own state database, ``state_<N>.sqlite``
in the home, and from the thread's rollout file. Both are first party and
current: the database names the thread, the repository and the model, and the
rollout says whether a turn is running right now and how full the context is.
The database is opened read only through a sqlite URI because Codex writes it
in WAL mode while it runs, and any error from it is read as "no rows": a schema
bump must never take the session list down with it.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import subprocess
import time
import urllib.parse
from dataclasses import dataclass

from . import codex, sessions, transcripts

# Subcommands that are not a session. Everything else is kept, which covers the
# bare TUI, `exec`, `review`, `resume`, `fork` and their aliases, including ones
# a newer CLI adds: a process that turns out to hold no thread lock is dropped
# later anyway, so guessing wrong here costs one lsof call and nothing else.
NOT_SESSIONS = frozenset({
    "login", "logout", "mcp", "mcp-server", "app-server", "remote-control", "app",
    "completion", "update", "doctor", "sandbox", "debug", "apply", "archive", "delete",
    "unarchive", "migrate-rollouts", "queue", "cloud", "exec-server", "features", "help",
    "plugin", "agents",
})

LOCKS_DIR = "thread-writer-locks"
LOCK_SUFFIX = ".lock"

# The columns of the `threads` table this reads. Named one by one rather than
# with a star so a column added upstream cannot shift the ones below it.
COLUMNS = ("id", "rollout_path", "created_at", "updated_at", "source", "thread_source",
           "cwd", "title", "name", "tokens_used", "model", "git_branch")

# A rollout's first line is its session_meta, and everything else worth reading
# is at the end, so the middle of a long conversation is never touched. One
# turn can write megabytes of tool output, though, and `task_started` is
# written before all of it: a busy `codex exec` was seen with its start 3.6 MB
# behind the end of the file, and read as idle. So a tail that answers neither
# question escalates to a wide one rather than reporting nothing.
HEAD_BYTES = 4_096
TAIL_BYTES = 65_536
WIDE_TAIL_BYTES = 6_400_000

# How long an lsof answer is trusted. The sessions tick runs every few seconds
# and would otherwise fork lsof once per Codex process on every pass, while the
# threads a process holds change on the scale of a conversation, not a tick.
FILES_TTL = 5.0

TITLE_CHARS = 80


def processes() -> list[tuple[int, str, str]]:
    """Every running Codex session process: (pid, start time, command line).

    One `ps` call for the whole machine, because the alternative is one per
    candidate. The start time comes back with the pid so a reused pid can be
    told from the process that held it before, which is what the caches below
    are keyed on.
    """
    try:
        out = subprocess.run(["ps", "-axo", "pid=,lstart=,command="],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        # `lstart` is five space separated fields ("Wed Sep 10 12:34:56 2026"),
        # so the command starts at the seventh.
        parts = line.split(None, 6)
        if len(parts) != 7 or not parts[0].isdigit():
            continue
        command = parts[6]
        if not codex.is_codex_command(command) or _subcommand(command) in NOT_SESSIONS:
            continue
        found.append((int(parts[0]), " ".join(parts[1:6]), command))
    return found


def _subcommand(command: str) -> str:
    """The first bare word after the executable: `codex exec -m x` says "exec".

    A flag that takes a value can put its value in that position, so this can
    answer with something that is no subcommand at all. That only ever keeps a
    process in the list, and the lock files decide in the end.
    """
    for word in command.split()[1:]:
        if not word.startswith("-"):
            return word
    return ""


_FILES: dict[tuple[int, str], tuple[float, tuple[str, list[str]]]] = {}


def open_files(pid: int, proc_start: str = "") -> tuple[str, list[str]]:
    """The working directory and the live thread ids of one Codex process.

    Both come out of a single `lsof`, in its field format so the answer needs
    no column parsing: `f` names the descriptor and the `n` after it names the
    file. A lock file listed here is one this process still holds, which is the
    only way to tell a live thread from a lock Codex has not cleaned up yet.
    """
    key = (pid, proc_start)
    now = time.monotonic()
    hit = _FILES.get(key)
    if hit is not None and now - hit[0] < FILES_TTL:
        return hit[1]
    cwd, threads_held, fd = "", [], ""
    try:
        out = subprocess.run(["lsof", "-p", str(pid), "-F", "fn"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            tag, name = line[:1], line[1:]
            if tag == "f":
                fd = name
            elif tag == "n":
                if fd == "cwd":
                    cwd = name
                elif (name.endswith(LOCK_SUFFIX)
                      and os.path.basename(os.path.dirname(name)) == LOCKS_DIR):
                    threads_held.append(os.path.basename(name)[:-len(LOCK_SUFFIX)])
    except Exception:  # noqa: BLE001 - a process we cannot read is not a fault
        return "", []
    if len(_FILES) > 256:
        _FILES.clear()
    _FILES[key] = (now, (cwd, threads_held))
    return cwd, threads_held


def state_db(home: str) -> str:
    """The newest state database in a home, or "" when it has none.

    The number in `state_<N>.sqlite` is a schema version, so the highest one
    present is the one the installed Codex writes. A session home reaches the
    real files through symlinks, hence the realpath first.
    """
    real = os.path.realpath(home)
    best, found = -1, ""
    try:
        entries = os.listdir(real)
    except OSError:
        return ""
    for name in entries:
        if not name.startswith("state_") or not name.endswith(".sqlite"):
            continue
        try:
            version = int(name[len("state_"):-len(".sqlite")])
        except ValueError:
            continue
        if version > best:
            best, found = version, os.path.join(real, name)
    return found


def threads(home: str, ids) -> dict[str, dict]:
    """What Codex's state database says about a set of thread ids.

    Read only, through the sqlite URI form, because Codex is writing this file
    in WAL mode while we look at it. Any sqlite error answers with no rows: the
    session list is worth more than the extra columns a newer schema might
    carry, and it has to survive one that drops them.
    """
    wanted = [i for i in ids if i]
    path = state_db(home)
    if not path or not wanted:
        return {}
    query = (f"SELECT {', '.join(COLUMNS)} FROM threads "
             f"WHERE id IN ({', '.join('?' * len(wanted))})")
    out: dict[str, dict] = {}
    try:
        uri = "file:" + urllib.parse.quote(path) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            for row in conn.execute(query, wanted):
                out[str(row[0])] = dict(zip(COLUMNS, row, strict=True))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - a database we cannot read has no rows
        return {}
    return out


@dataclass
class Digest:
    """What one thread's rollout file says about the turn it is in."""
    started_at: float = 0.0
    context_tokens: int = 0
    context_window: int = 0
    spent_tokens: int = 0
    model: str = ""
    busy: bool = False
    updated_at: float = 0.0


_digests: dict[tuple[str, int, float], Digest] = {}


def digest(path: str) -> Digest:
    """Read one rollout's head and tail, and cache it against the file.

    The head holds the session_meta line and nothing else that matters; the
    tail holds the model, the token counts and the turn events. A rollout is
    append only, so size and mtime together say whether a cached answer still
    describes it, which makes a tick that changed nothing free.
    """
    if not path:
        return Digest()
    try:
        st = os.stat(path)
    except OSError:
        return Digest()
    key = (path, st.st_size, st.st_mtime)
    if key in _digests:
        return _digests[key]
    out = Digest(updated_at=st.st_mtime)
    try:
        with open(path, "rb") as f:
            _read_meta(out, f.read(HEAD_BYTES))
            if (not _read_tail(out, _tail(f, st.st_size, TAIL_BYTES))
                    and st.st_size > TAIL_BYTES):
                _read_tail(out, _tail(f, st.st_size, WIDE_TAIL_BYTES))
    except OSError:
        return out
    if len(_digests) > 256:
        _digests.clear()
    _digests[key] = out
    return out


def _tail(f, size: int, want: int) -> bytes:
    if size > want:
        f.seek(-want, os.SEEK_END)
        f.readline()                  # drop the partial line the seek landed in
    else:
        f.seek(0)
    return f.read()


def _record(line: bytes) -> dict:
    try:
        d = json.loads(line)
    except ValueError:
        return {}                     # a half-written last line is normal
    return d if isinstance(d, dict) else {}


def _read_meta(out: Digest, head: bytes) -> None:
    """session_meta is always the first line, so only that one is parsed."""
    first = head.splitlines()[:1]
    if not first:
        return
    d = _record(first[0])
    if d.get("type") != "session_meta":
        return
    payload = d.get("payload") or {}
    out.started_at = _stamp(payload.get("timestamp") or d.get("timestamp"))
    out.context_window = _int(payload.get("context_window"))


def _read_tail(out: Digest, tail: bytes) -> bool:
    """Read a rollout's tail, and say whether it answered both questions.

    Both being the turn the thread is in and how full its context is. A tail
    that answered neither is too short for this rollout, and the caller reads
    a wider one rather than calling a busy thread idle.
    """
    turn = False
    for line in tail.splitlines():
        d = _record(line)
        payload = d.get("payload") or {}
        kind = d.get("type")
        if kind == "turn_context":
            out.model = payload.get("model") or out.model
        elif kind == "event_msg":
            event = payload.get("type")
            if event in ("task_started", "task_complete", "turn_aborted"):
                # The last of the three is the state the thread is in now.
                out.busy, turn = event == "task_started", True
            elif event == "token_count":
                info = payload.get("info")
                if isinstance(info, dict):
                    # `info` is null between turns, and a null one says nothing
                    # about the counts, so the last one that carried them wins.
                    last = info.get("last_token_usage") or {}
                    total = info.get("total_token_usage") or {}
                    out.context_tokens = _int(last.get("input_tokens")) or out.context_tokens
                    out.spent_tokens = _int(total.get("total_tokens")) or out.spent_tokens
                    out.context_window = (_int(info.get("model_context_window"))
                                          or out.context_window)
    return turn and bool(out.context_tokens)


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _stamp(value) -> float:
    """An ISO 8601 timestamp as epoch seconds, or 0 when it cannot be read."""
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        when = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return when.timestamp()


def _title(row: dict) -> str:
    """The thread's first user message, cut to something a row can hold."""
    lines = str(row.get("title") or "").strip().splitlines()
    return lines[0].strip()[:TITLE_CHARS] if lines else ""


def _main_thread(rows: dict[str, dict], ids) -> dict | None:
    """The thread a process IS, as opposed to the subagents it spawned.

    Subagent threads are held by the same process and spend the same account,
    so they belong on this row rather than on rows of their own. The user's
    own thread is the one the row describes.
    """
    ordered = [rows[i] for i in ids if i in rows]
    if not ordered:
        return None
    return next((r for r in ordered if r.get("thread_source") == "user"), ordered[0])


def _spent(rows: dict[str, dict], main: dict, floor: int) -> transcripts.Totals:
    """Every token this process has spent, its subagents included.

    A subagent thread was started on this session's behalf and bills the same
    account, so its tokens are this row's tokens. The database is written at
    the end of a turn and the rollout during it, so the rollout's own count is
    the floor for the main thread rather than being ignored.

    It lands in `input` because Codex reports one total and no split: a Totals
    built here has one number to give, and `total` is what a row reads.
    """
    main_id = str(main.get("id") or "")
    other = sum(_int(r.get("tokens_used")) for i, r in rows.items() if i != main_id)
    return transcripts.Totals(input=other + max(_int(main.get("tokens_used")), floor))


def _session(pid: int, proc_start: str) -> sessions.Session | None:
    cwd, ids = open_files(pid, proc_start)
    if not ids:
        return None                   # holds no thread lock: not a live session
    env, tty = sessions._environ(pid, proc_start)
    home = os.path.abspath(env.get("CODEX_HOME") or codex.DEFAULT_HOME)
    rows = threads(home, ids)
    main = _main_thread(rows, ids)
    if main is None:
        return None                   # nothing first party to say about it
    g = digest(str(main.get("rollout_path") or ""))
    window = g.context_window
    pct = None
    if g.context_tokens and window:
        pct = min(100.0, g.context_tokens / window * 100)
    name = str(main.get("name") or "")
    return sessions.Session(
        pid=pid,
        config_dir=home,
        provider=codex.PROVIDER,
        proc_start=proc_start,
        session_id=str(main.get("id") or ""),
        cwd=cwd or str(main.get("cwd") or ""),
        name=name,
        # `cli` is the interactive TUI; `exec` and the subagent sources are not
        # somebody sitting in front of it.
        kind="interactive" if main.get("source") == "cli" else "bg",
        status="busy" if g.busy else "idle",
        started_at=g.started_at or _float(main.get("created_at")),
        updated_at=max(_float(main.get("updated_at")), g.updated_at),
        term_id=env.get("TERM_SESSION_ID", ""),
        term_program=env.get("TERM_PROGRAM", ""),
        tty=tty,
        env_config_dir=home,
        name_source="user" if name else "derived",
        title=_title(main),
        context_tokens=g.context_tokens,
        context_window=window,
        model=g.model or str(main.get("model") or ""),
        context_pct=pct,
        spent=_spent(rows, main, g.spent_tokens),
    )


def live(with_git: bool = False) -> list[sessions.Session]:
    """Every running Codex session, newest first.

    One Session per process, not per thread: a process holds its subagents'
    threads too, and they are part of what that session is doing rather than
    sessions of their own. A process that cannot be read is skipped, because a
    session list that raises is worse than one that is one row short.
    """
    out = []
    for pid, proc_start, _command in processes():
        try:
            found = _session(pid, proc_start)
        except Exception:  # noqa: BLE001 - one unreadable process, not a failure
            continue
        if found is not None:
            out.append(found)
    if with_git:
        for s in out:
            s.branch = sessions.branch_of(s.cwd) if s.cwd else ""
    return sorted(out, key=lambda s: s.updated_at, reverse=True)
