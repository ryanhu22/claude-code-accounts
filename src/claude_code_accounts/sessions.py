"""Live Claude Code sessions, read from Claude Code's own registry.

Claude Code writes ``<config dir>/sessions/<pid>.json`` for every session it
starts, carrying the pid, session id, cwd, kind, entrypoint, status and the
name it shows in its own session list. It is first-party and current, so it
beats inferring activity from transcript timestamps, and it lives inside the
config dir, which is exactly how a session is attributed to an account.

What it does not record is the terminal the session runs in. That comes from
the process environment, where ``TERM_SESSION_ID`` is a UUID the terminal
assigns per tab. It is the only identifier here that survives restarting Claude
Code in the same tab and is never recycled the way a pid or a tty number is, so
it is what a per-session account pin is keyed to.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from . import transcripts

# A session file is only rewritten while its session lives, so a stale one is
# just litter from a process that died without cleaning up.
STALE_AFTER = 7 * 24 * 3600


@dataclass
class Session:
    pid: int
    config_dir: str              # the dir whose registry listed it: its account
    provider: str = "claude"     # claude | codex: which tool is running here
    proc_start: str = ""         # tells a reused pid from the one before it
    session_id: str = ""
    cwd: str = ""
    name: str = ""
    kind: str = ""               # interactive | bg | daemon
    entrypoint: str = ""
    status: str = ""             # idle | busy | shell
    started_at: float = 0.0
    updated_at: float = 0.0
    term_id: str = ""            # terminal tab, from the process environment
    term_program: str = ""       # TERM_PROGRAM: which terminal app hosts it
    tty: str = ""                # controlling terminal, e.g. ttys003
    env_config_dir: str = ""     # CLAUDE_CONFIG_DIR the process actually launched with
    name_source: str = ""        # "user" when named deliberately, else "derived"
    branch: str = ""
    title: str = ""              # Claude Code's own description of the conversation
    context_tokens: int = 0
    context_window: int = 0      # the window the session itself reported, 0 if it did not
    model: str = ""
    context_pct: float | None = None
    spent: transcripts.Totals = field(default_factory=lambda: transcripts.Totals())

    @property
    def is_codex(self) -> bool:
        return self.provider == "codex"

    @property
    def is_worktree(self) -> bool:
        parts = self.cwd.rstrip("/").split("/")
        return ".claude" in parts and "worktrees" in parts

    @property
    def repo(self) -> str:
        """Repository name: a worktree reports its parent repo, not its own dir."""
        parts = self.cwd.rstrip("/").split("/")
        if self.is_worktree:
            return parts[max(0, parts.index("worktrees") - 2)]
        return parts[-1] or self.cwd

    @property
    def window(self) -> int:
        """The context window, as the session reported it or by its model.

        Codex names its own window in the rollout, which beats any table: it
        knows what the server gave that thread. Claude Code says nothing about
        it, so its model is looked up the way it always was.
        """
        return self.context_window or transcripts.window_for(self.model)

    @property
    def derived_name(self) -> bool:
        """True when the name is Claude Code's placeholder, not a real one.

        A derived name is the repo plus a few hex characters ("acme-app-9d"),
        which says nothing at all once the repo is already its own column.
        """
        return self.name_source == "derived" or not self.name

    @property
    def detail(self) -> str:
        """What tells this session apart from its siblings.

        Worktrees of one repo differ by branch, and that is also which worktree
        you are looking at, so it wins there. Sessions sharing a checkout are
        all on main instead, and are told apart by what they are about: the
        name if one was chosen, otherwise the title Claude Code wrote for the
        conversation.
        """
        if self.is_worktree:
            return self.branch or self.cwd.rstrip("/").split("/")[-1]
        if not self.derived_name:
            name, repo = self.name, self.repo
            if name.lower().startswith(repo.lower() + "-"):
                name = name[len(repo) + 1:]
            if name:
                return name
        return self.title or self.branch or self.name

    @property
    def interactive(self) -> bool:
        return self.kind == "interactive"

    @property
    def age(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0

    @property
    def idle_for(self) -> float:
        return time.time() - self.updated_at if self.updated_at else 0.0

    @property
    def label(self) -> str:
        """What to call this session. Claude Code's own name, else the folder."""
        return self.name or os.path.basename(self.cwd.rstrip("/")) or f"pid {self.pid}"


def alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # someone else's process, but it exists
    return True


_ENV_CACHE: dict[tuple[int, str], tuple[dict[str, str], str]] = {}


def _environ(pid: int, proc_start: str = "") -> tuple[dict[str, str], str]:
    """The environment a running process was started with, and its tty.

    `ps eww` prints both for our own processes, which is every Claude Code
    session we care about. Values are space separated, so a value containing a
    space is truncated; the keys read here never contain one. The tty is the
    second column of the process line; a process with no terminal shows `??`.
    """
    # Neither answer can change while the process lives, so this is asked once
    # per process rather than on every pass. The start time is part of the key
    # because pids are reused, and the answer must not survive onto another
    # process that happens to get the same one.
    key = (pid, proc_start)
    if key in _ENV_CACHE:
        return _ENV_CACHE[key]
    try:
        out = subprocess.run(["ps", "eww", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}, ""
    lines = out.splitlines()
    tty = ""
    if len(lines) > 1:
        cols = lines[1].split()
        if len(cols) > 1 and cols[1] != "??":
            # ps abbreviates `ttys013` to `s013`; the terminal's own report
            # of a tab's tty is the long form, so keep that form here.
            tty = cols[1] if cols[1].startswith("tty") else "tty" + cols[1]
    env = {}
    for word in out.split():
        name, sep, val = word.partition("=")
        if sep and name.isupper() and name.replace("_", "").isalnum():
            env[name] = val
    if len(_ENV_CACHE) > 256:
        _ENV_CACHE.clear()
    _ENV_CACHE[key] = (env, tty)
    return env, tty


def _read(path: str, config_dir: str) -> Session | None:
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    pid = d.get("pid")
    if not isinstance(pid, int) or not alive(pid):
        return None
    return Session(
        proc_start=d.get("procStart") or "",
        pid=pid,
        config_dir=config_dir,
        session_id=d.get("sessionId") or "",
        cwd=d.get("cwd") or "",
        name=d.get("name") or "",
        kind=d.get("kind") or "",
        entrypoint=d.get("entrypoint") or "",
        name_source=d.get("nameSource") or "",
        status=d.get("status") or "",
        started_at=(d.get("startedAt") or 0) / 1000,
        updated_at=(d.get("updatedAt") or d.get("startedAt") or 0) / 1000,
    )


_BRANCH_CACHE: dict[str, tuple[float, str]] = {}


def _head_stamp(path: str) -> float:
    """When this checkout's HEAD last moved, or 0 if it cannot be told.

    Checking out a branch rewrites HEAD, so its mtime says whether the cached
    answer is still good without running git at all.
    """
    for head in (os.path.join(path, ".git", "HEAD"), os.path.join(path, ".git")):
        try:
            if os.path.isfile(head):
                return os.path.getmtime(head)
            if os.path.isfile(os.path.join(path, ".git")):   # a worktree's pointer
                with open(os.path.join(path, ".git")) as f:
                    gitdir = f.read().strip().split("gitdir:", 1)[-1].strip()
                return os.path.getmtime(os.path.join(gitdir, "HEAD"))
        except OSError:
            continue
    return 0.0


def branch_of(path: str) -> str:
    """Checked-out branch, or "" when detached or not a repo."""
    stamp = _head_stamp(path)
    hit = _BRANCH_CACHE.get(path)
    if stamp and hit and hit[0] == stamp:
        return hit[1]
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    name = r.stdout.strip() if r.returncode == 0 else ""
    branch = name if name and name != "HEAD" else ""
    if stamp:
        if len(_BRANCH_CACHE) > 256:
            _BRANCH_CACHE.clear()
        _BRANCH_CACHE[path] = (stamp, branch)
    return branch


def transcript_roots(config_dirs: Iterable[str]) -> list[str]:
    """The distinct projects/ trees behind a set of config dirs.

    Contexts symlink projects/ back to ~/.claude so history stays in one place,
    so resolving the link first keeps this to a single tree in practice.
    """
    roots = {os.path.realpath(os.path.join(d, "projects")) for d in config_dirs}
    return sorted(r for r in roots if os.path.isdir(r))


def live(config_dirs: Iterable[str], with_env: bool = True,
         with_git: bool = False, with_transcript: bool = False) -> list[Session]:
    """Every running session across the given config dirs, newest first.

    A pid can appear in two registries when a session moved between config
    dirs; the environment says which one it really launched with, so that copy
    wins and the other is dropped.
    """
    found: dict[int, Session] = {}
    for cfg in config_dirs:
        for path in glob.glob(os.path.join(cfg, "sessions", "*.json")):
            s = _read(path, cfg)
            if s and s.pid not in found:
                found[s.pid] = s
    out = list(found.values())
    if with_env:
        for s in out:
            env, s.tty = _environ(s.pid, s.proc_start)
            s.term_id = env.get("TERM_SESSION_ID", "")
            s.term_program = env.get("TERM_PROGRAM", "")
            s.env_config_dir = env.get("CLAUDE_CONFIG_DIR", "") or s.config_dir
    if with_git:
        for s in out:
            s.branch = branch_of(s.cwd) if s.cwd else ""
    if with_transcript:
        roots = transcript_roots(config_dirs)
        for s in out:
            path = transcripts.find(s.session_id, roots)
            g = transcripts.digest(path)
            s.title, s.context_tokens = g.title, g.context_tokens
            s.model, s.context_pct = g.model, g.context_pct
            s.spent = transcripts.lifetime(path, save=False)
        transcripts.flush()
    return sorted(out, key=lambda s: s.updated_at, reverse=True)


def prune(config_dirs: Iterable[str]) -> int:
    """Delete registry files left behind by sessions that died long ago."""
    removed, cutoff = 0, time.time() - STALE_AFTER
    for cfg in config_dirs:
        for path in glob.glob(os.path.join(cfg, "sessions", "*.json")):
            try:
                d = json.load(open(path))
                if alive(d.get("pid") or 0) or os.path.getmtime(path) > cutoff:
                    continue
                os.remove(path)
                removed += 1
            except (OSError, ValueError):
                continue
    return removed


def discover_config_dirs(home: str | None = None) -> list[str]:
    """Config dirs with a session running right now.

    Claude Code keys its keychain item on the config-dir path STRING, so two
    paths naming the same directory are two separate logins: `~/.claude-work`
    is a symlink to `~/.claude-acme` and has a login of its own.
    A dir reached only through such an alias appears in no routing table while
    still billing real work, so it is found here by its live session files.
    """
    home = home or os.path.expanduser("~")
    found = []
    for pattern in (".claude", ".claude-*", ".claude-ctx/*"):
        for d in sorted(glob.glob(os.path.join(home, pattern))):
            if not os.path.isdir(d):
                continue
            if any(_read(f, d) for f in glob.glob(os.path.join(d, "sessions", "*.json"))):
                found.append(d)
    return found
