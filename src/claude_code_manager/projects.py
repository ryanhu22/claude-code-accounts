"""Which projects have had Claude Code activity recently.

Claude Code writes one JSONL transcript per session under
``<config dir>/projects/<slug>/<session>.jsonl`` and appends to it as the
session runs, so file mtime is a reliable "this project was active" signal.
The real working directory is read out of the transcript rather than decoded
from the slug, which is lossy (both "/" and "-" become "-").
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from .core import HOME, Context, context_for, contexts


def branch_of(path: str) -> Optional[str]:
    """Checked-out branch, or None when detached or not a repo."""
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    name = r.stdout.strip() if r.returncode == 0 else ""
    return name or None if name != "HEAD" else None


@dataclass
class ProjectActivity:
    path: str          # working directory of the session
    last_active: float # epoch seconds
    sessions: int
    context: Context
    branch: Optional[str] = None

    @property
    def is_worktree(self) -> bool:
        parts = self.path.rstrip("/").split("/")
        return ".claude" in parts and "worktrees" in parts

    @property
    def repo(self) -> str:
        """Repository name: a worktree reports its parent repo, not its own dir."""
        parts = self.path.rstrip("/").split("/")
        if self.is_worktree:
            return parts[max(0, parts.index("worktrees") - 2)]
        return parts[-1] or self.path

    @property
    def detail(self) -> str:
        """What distinguishes this session inside the repo: its branch."""
        if self.branch:
            return self.branch
        return "" if not self.is_worktree else self.path.rstrip("/").split("/")[-1]

    @property
    def name(self) -> str:
        """Readable label.

        Worktree directories are generated names ("snuggly-hopping-falcon")
        that say nothing about the work, so a worktree is labelled with its
        parent repo and its branch, falling back to the directory name when
        the branch is unavailable (detached HEAD, or git not answering).
        """
        parts = self.path.rstrip("/").split("/")
        if self.is_worktree:
            i = parts.index("worktrees")
            repo = parts[max(0, i - 2)]
            return f"{repo} / {self.branch or parts[-1]}"
        return parts[-1] or self.path

    @property
    def ago(self) -> str:
        mins = int((time.time() - self.last_active) // 60)
        if mins < 1:
            return "just now"
        if mins < 60:
            return f"{mins}m ago"
        return f"{mins // 60}h {mins % 60}m ago"


def _cwd_of(transcript: str) -> Optional[str]:
    """Read the session's cwd from the tail of its transcript."""
    try:
        size = os.path.getsize(transcript)
        with open(transcript, "rb") as f:
            f.seek(max(0, size - 65536))
            chunk = f.read().decode("utf-8", "ignore")
    except OSError:
        return None
    for line in reversed(chunk.splitlines()):
        if '"cwd"' not in line:
            continue
        try:
            cwd = json.loads(line).get("cwd")
        except ValueError:
            continue
        if cwd:
            return cwd
    return None


def _project_roots() -> list[str]:
    roots, seen = [], set()
    for ctx in contexts():
        p = os.path.realpath(os.path.join(ctx.path, "projects"))
        if p not in seen and os.path.isdir(p):
            roots.append(p)
            seen.add(p)
    return roots


def recent(minutes: int = 60) -> list[ProjectActivity]:
    """Projects with session activity in the last `minutes`, newest first."""
    cutoff = time.time() - minutes * 60
    found: dict[str, ProjectActivity] = {}
    for root in _project_roots():
        try:
            slugs = os.listdir(root)
        except OSError:
            continue
        for slug in slugs:
            sdir = os.path.join(root, slug)
            try:
                files = [os.path.join(sdir, f) for f in os.listdir(sdir) if f.endswith(".jsonl")]
            except OSError:
                continue
            live = [f for f in files if _mtime(f) >= cutoff]
            if not live:
                continue
            newest = max(live, key=_mtime)
            cwd = _cwd_of(newest) or _slug_guess(slug)
            if not cwd:
                continue
            existing = found.get(cwd)
            mt = _mtime(newest)
            if existing:
                existing.sessions += len(live)
                existing.last_active = max(existing.last_active, mt)
            else:
                found[cwd] = ProjectActivity(path=cwd, last_active=mt, sessions=len(live),
                                             context=context_for(cwd), branch=branch_of(cwd))
    return sorted(found.values(), key=lambda p: p.last_active, reverse=True)


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _slug_guess(slug: str) -> Optional[str]:
    """Fallback when a transcript has no cwd line: try the slug as a path."""
    cand = "/" + slug.lstrip("-").replace("-", "/")
    return cand if os.path.isdir(cand) else None
