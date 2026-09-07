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
from collections.abc import Iterable
from dataclasses import dataclass

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
class Totals:
    """Everything one session has spent, over its whole life.

    Kept as four separate figures because they are not interchangeable: a cache
    read is a fraction of the price of a fresh input token, and a long
    conversation re-reads its whole cache every turn, so cache_read dominates
    the sum and would flatter any single "tokens used" number that hid it.
    """
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    turns: int = 0

    @property
    def total(self) -> int:
        return self.input + self.cache_write + self.cache_read + self.output

    @property
    def fresh(self) -> int:
        """Everything but cache reads: the part that was actually processed."""
        return self.input + self.cache_write + self.output


TOKEN_CACHE = os.path.join(os.path.expanduser("~"), ".claude-accts", ".tokens.json")
_tokens: dict | None = None
_dirty = False


def _tokens_load() -> dict:
    global _tokens
    if _tokens is None:
        try:
            with open(TOKEN_CACHE) as f:
                _tokens = json.load(f)
        except (OSError, ValueError):
            _tokens = {}
    return _tokens


def _tokens_save() -> None:
    try:
        os.makedirs(os.path.dirname(TOKEN_CACHE), exist_ok=True)
        tmp = TOKEN_CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_tokens_load(), f)
        os.replace(tmp, TOKEN_CACHE)
    except OSError:
        pass


def flush() -> None:
    """Save once after a scan, if any transcript changed."""
    global _dirty
    if _dirty:
        _tokens_save()
        _dirty = False


def lifetime(path: str, save: bool = True) -> Totals:
    """Every token this session has spent, counted once.

    A transcript is append only, so the count is resumed from where the last
    pass stopped rather than rebuilt: only the bytes written since then are
    read. The offset is kept on disk, so this survives a restart too, and the
    first pass over a large transcript happens once ever.
    """
    global _dirty
    if not path:
        return Totals()
    try:
        size = os.path.getsize(path)
    except OSError:
        return Totals()
    store = _tokens_load()
    e = store.get(path) or {}
    offset = int(e.get("offset") or 0)
    if offset > size:
        e, offset = {}, 0          # rewritten or replaced: start over
    t = Totals(input=int(e.get("input") or 0), cache_write=int(e.get("cache_write") or 0),
               cache_read=int(e.get("cache_read") or 0), output=int(e.get("output") or 0),
               turns=int(e.get("turns") or 0))
    if offset == size:
        return t
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            for line in f:
                if not line.endswith(b"\n"):
                    break          # a half-written last line; count it next time
                offset += len(line)
                if b'"usage"' not in line:
                    continue       # cheap filter: most records carry no usage
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "assistant":
                    continue
                u = ((d.get("message") or {}).get("usage")) or {}
                t.input += int(u.get("input_tokens") or 0)
                t.cache_write += int(u.get("cache_creation_input_tokens") or 0)
                t.cache_read += int(u.get("cache_read_input_tokens") or 0)
                t.output += int(u.get("output_tokens") or 0)
                t.turns += 1
    except OSError:
        return t
    store[path] = {"offset": offset, "input": t.input, "cache_write": t.cache_write,
                   "cache_read": t.cache_read, "output": t.output, "turns": t.turns}
    if len(store) > 500:
        for gone in [k for k in store if not os.path.exists(k)]:
            del store[gone]
    _dirty = True
    if save:
        flush()
    return t


@dataclass
class Digest:
    title: str = ""
    context_tokens: int = 0
    model: str = ""

    @property
    def window(self) -> int:
        return window_for(self.model)

    @property
    def context_pct(self) -> float | None:
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
