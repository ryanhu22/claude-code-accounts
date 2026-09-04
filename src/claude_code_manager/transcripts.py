"""What a session's transcript says about it: its title and how full it is.

Claude Code appends two useful records to every session's JSONL. ``ai-title``
carries the short description it generates for the conversation, which is what
its own session list shows once it has one. Each ``assistant`` record carries a
``usage`` block whose input side (fresh, cache reads and cache writes together)
is the size of the context that request sent, so the last one is how full the
session is right now.

Both live near the end of the file, so a tail read answers both without opening
a transcript that can run to tens of megabytes. Results are cached against the
file's size and mtime, which makes a refresh that changed nothing free.
"""
from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass
from typing import Iterable, Optional

# Enough tail to hold several assistant turns and the periodic title record.
# A single record can be far larger than a turn (a pasted image, a big file
# read), so a miss escalates to the wider window rather than reporting nothing.
TAIL_BYTES = 400_000
WIDE_TAIL_BYTES = 6_400_000

# Context windows by model family. A wrong guess only skews the bar, never the
# token count beside it, so an unknown model takes the conservative window.
WINDOWS = ((("opus-5", "sonnet-5", "fable"), 1_000_000),
           (("haiku",), 200_000))
DEFAULT_WINDOW = 200_000


def window_for(model: str) -> int:
    model = (model or "").lower()
    for names, size in WINDOWS:
        if any(n in model for n in names):
            return size
    return DEFAULT_WINDOW


@dataclass
class Digest:
    title: str = ""
    context_tokens: int = 0
    model: str = ""

    @property
    def window(self) -> int:
        return window_for(self.model)

    @property
    def context_pct(self) -> Optional[float]:
        if not self.context_tokens:
            return None
        return min(100.0, self.context_tokens / self.window * 100)


_paths: dict[str, str] = {}
_digests: dict[tuple[str, int, float], Digest] = {}


def find(session_id: str, roots: Iterable[str]) -> str:
    """The transcript file for a session id.

    Searched rather than derived: the project folder is named for a slug of the
    working directory, but a worktree session can be filed under its parent
    repo, so building the name from the cwd gets it wrong. A transcript never
    moves, so the answer is remembered for good.
    """
    if not session_id:
        return ""
    if session_id in _paths:
        return _paths[session_id]
    for root in roots:
        hits = glob.glob(os.path.join(root, "*", session_id + ".jsonl"))
        if hits:
            _paths[session_id] = hits[0]
            return hits[0]
    return ""


def digest(path: str) -> Digest:
    """Title and context size from the tail of one transcript."""
    if not path:
        return Digest()
    try:
        st = os.stat(path)
    except OSError:
        return Digest()
    key = (path, st.st_size, st.st_mtime)
    if key in _digests:
        return _digests[key]
    out = _scan(path, st.st_size, TAIL_BYTES)
    if not out.context_tokens and st.st_size > TAIL_BYTES:
        out = _scan(path, st.st_size, WIDE_TAIL_BYTES) or out
    if len(_digests) > 256:
        _digests.clear()
    _digests[key] = out
    return out


def _scan(path: str, size: int, tail: int) -> Digest:
    out = Digest()
    try:
        with open(path, "rb") as f:
            if size > tail:
                f.seek(-tail, os.SEEK_END)
                f.readline()          # drop the partial line the seek landed in
            chunk = f.read()
    except OSError:
        return out
    for line in chunk.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue                  # a half-written last line is normal
        kind = d.get("type")
        if kind == "ai-title" and d.get("aiTitle"):
            out.title = str(d["aiTitle"])
        elif kind == "assistant":
            msg = d.get("message") or {}
            usage = msg.get("usage") or {}
            total = sum(int(usage.get(k) or 0) for k in
                        ("input_tokens", "cache_read_input_tokens",
                         "cache_creation_input_tokens"))
            if total:
                out.context_tokens = total
                out.model = msg.get("model") or out.model
    return out
