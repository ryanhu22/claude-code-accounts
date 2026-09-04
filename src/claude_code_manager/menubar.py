"""macOS menu bar app for managing Claude Code subscriptions.

Shows every subscription's usage, which projects are running on which account,
swaps a project's account in one click, pokes an idle account to start its
5-hour window, and adds or removes accounts.

Threading model: a background worker fetches usage (network), and a fast timer
on the main thread applies the result and rebuilds the menu only when
something changed. AppKit is not thread-safe, so no menu object is ever
touched off the main thread.
"""
from __future__ import annotations

import datetime as _dt
import os
import subprocess
import threading
import time
import webbrowser
from typing import Optional

import rumps

from . import core, focus, gauge, oauth, sessions

REFRESH_SECONDS = 180      # usage is not fast-moving; stay light on the API
CREDENTIAL_SYNC_SECONDS = 45   # local only: keeps every copy of a login alive
SESSION_POLL_SECONDS = 5       # local only: how soon a new session appears
FLASH_SECONDS = 30             # how long the last rule change stays on screen
ICON = "⇄"
FOCUS_MARK = "\u25b8"      # ▸ the session whose tab is in front


# Menu rows are drawn as attributed strings so the three usage buckets line up
# in real columns. A proportional font cannot align with spaces, and a plain
# title cannot colour the bucket that is nearly spent.
NAME_W = 13                # the longest account name, so chips form a column
REPO_W = 12
DETAIL_W = 24
CTX_BAR_W = 6              # the one gauge left in a row, and the only one asked for
PROFILE_W = 16


def _fit(text: str, width: int) -> str:
    """Pad or truncate to an exact width so columns cannot be knocked askew."""
    if len(text) <= width:
        return text.ljust(width)
    return text[: max(1, width - 1)] + "\u2026"
FULL, EMPTY = "\u2588", "\u2591"          # █ ░


def _colors():
    import AppKit
    return {
        "ok": AppKit.NSColor.systemGreenColor(),
        "warn": AppKit.NSColor.systemOrangeColor(),
        "hot": AppKit.NSColor.systemRedColor(),
        "dim": AppKit.NSColor.secondaryLabelColor(),
        "text": AppKit.NSColor.labelColor(),
    }


# Each account keeps one colour everywhere it appears, so a glance at the
# project list tells you which subscription is paying without reading names.
# Keyed by a hash of the name so colours stay put when accounts are added.
# Twelve hues spread around the wheel, ORDERED so that sequential assignment
# is maximally distinct: the first five accounts get blue, orange, green,
# magenta, cyan rather than five neighbouring blues. Saturation and brightness
# are tuned to survive both the 22% background wash and the light-mode blend.
CHIP_COLORS = (
    ("Blue",    0.58, 0.80, 0.95),
    ("Orange",  0.07, 0.85, 0.98),
    ("Green",   0.33, 0.75, 0.80),
    ("Magenta", 0.85, 0.70, 0.92),
    ("Cyan",    0.51, 0.75, 0.88),
    ("Crimson", 0.99, 0.75, 0.92),
    ("Olive",   0.18, 0.80, 0.78),
    ("Violet",  0.74, 0.65, 0.95),
    ("Teal",    0.46, 0.75, 0.78),
    ("Amber",   0.12, 0.85, 0.92),
    ("Pink",    0.93, 0.55, 0.98),
    ("Indigo",  0.66, 0.70, 0.90),
)


def _chip_color(name: str):
    import AppKit
    if name.startswith("__palette"):
        idx = int(name.removeprefix("__palette")) % len(CHIP_COLORS)
    else:
        idx = core.chip_index(name, len(CHIP_COLORS))
    _, hue, sat, bri = CHIP_COLORS[idx]
    return AppKit.NSColor.colorWithHue_saturation_brightness_alpha_(hue, sat, bri, 1.0)


def _chip(name: str, width: int = 0) -> list[tuple[str, str, str]]:
    """A filled rectangle behind the account name, like a terminal badge.

    The padding that squares the column sits OUTSIDE the fill. Putting it
    inside made every chip the width of the longest account name, so a short
    name floated in a block of colour and the eye read the block instead of
    the word.
    """
    out = [(f" {name} ", "chip_fg", name)]
    if width and len(name) < width:
        out.append((" " * (width - len(name)), "dim"))
    return out


def _tone(pct: Optional[float]) -> str:
    if pct is None:
        return "dim"
    return "ok" if pct < 60 else "warn" if pct < 85 else "hot"


def _styled(segments, size: float = 12.0):
    """Build an NSAttributedString from runs.

    A run is (text, tone) or (text, tone, chip_key); with a chip key the run is
    drawn on a filled background in that account's colour.
    """
    import AppKit
    colors = _colors()
    font = AppKit.NSFont.monospacedSystemFontOfSize_weight_(size, AppKit.NSFontWeightRegular)
    out = AppKit.NSMutableAttributedString.alloc().init()
    for run in segments:
        text, tone = run[0], run[1]
        chip_key = run[2] if len(run) > 2 else None
        attrs = {AppKit.NSFontAttributeName: font}
        if chip_key:
            # Terminal-style badge: a faint wash of the hue behind text drawn in
            # that same hue. A solid fill with white text loses badly on the
            # lighter hues, and fails outright in light mode.
            base = _chip_color(chip_key)
            attrs[AppKit.NSBackgroundColorAttributeName] = base.colorWithAlphaComponent_(0.22)
            attrs[AppKit.NSForegroundColorAttributeName] = base.blendedColorWithFraction_ofColor_(
                0.42, AppKit.NSColor.labelColor()) or base
        else:
            attrs[AppKit.NSForegroundColorAttributeName] = colors.get(tone, colors["text"])
        out.appendAttributedString_(
            AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs))
    return out


def _apply_style(item: "rumps.MenuItem", segments) -> None:
    """Style a row, falling back silently to its plain title if AppKit balks."""
    try:
        item._menuitem.setAttributedTitle_(_styled(segments))
    except Exception:
        pass


def _compact_reset(iso: Optional[str]) -> str:
    """Short countdown for an inline row: 45m, 4h, 34h, 3d.

    Computed from the absolute reset timestamp on every render, so it stays
    right between refreshes instead of ageing with the fetch.
    """
    if not iso:
        return "idle"
    try:
        dt = _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    mins = round((dt - _dt.datetime.now(_dt.timezone.utc)).total_seconds() / 60)
    if mins <= 0:
        return "now"
    if mins < 60:
        return f"{mins}m"
    hours = round(mins / 60)
    if hours < 48:
        return f"{hours}h"
    return f"{round(hours / 24)}d"


def _reset_tone(lim: Optional[core.Limit]) -> str:
    """How much the countdown matters, not merely how long it is.

    A far-off reset on a barely-used bucket is noise, so it stays dim. Once a
    bucket is nearly spent the countdown becomes the number you care about:
    green if relief is close, red if you are locked out for a long while.
    An idle window is green because that account can be poked.
    """
    if lim is None:
        return "dim"
    if not lim.resets_at:
        return "ok"
    if lim.spent < 85:
        return "dim"
    try:
        dt = _dt.datetime.fromisoformat(str(lim.resets_at).replace("Z", "+00:00"))
    except ValueError:
        return "dim"
    mins = (dt - _dt.datetime.now(_dt.timezone.utc)).total_seconds() / 60
    if mins < 60:
        return "ok"
    if mins < 360:
        return "warn"
    return "hot"


def _quiet(tone: str) -> str:
    """Let a healthy value be plain text.

    Green marked "nothing is wrong", which is nearly every number, so the menu
    was mostly green and the one figure that mattered had to compete with it.
    Only warn and hot keep a colour, and they now mean one thing. The menu bar
    image keeps its green, because a battery there has no text beside it.
    """
    return "text" if tone == "ok" else tone


def _bucket(label: str, lim: Optional[core.Limit], show_reset: bool = True) -> list[tuple[str, str]]:
    """One usage window: its name, how much is gone, and when it comes back.

    There used to be a ten cell bar in front of the number. It said the same
    thing to one tenth the precision and took three times the width, and three
    of them made an account row 912 points wide, half the screen. The number
    is the datum and its colour carries the level.
    """
    pct = lim.spent if lim else None
    tone = _quiet(_tone(pct))
    out = [(f"  {label:<5}", "dim"),
           ("   —" if pct is None else f"{pct:3.0f}%", tone)]
    if show_reset:
        # B. A middle dot, not the ↻ used in the menu bar image: SF Mono has no
        # ↻, so it came from a fallback font at a different width and drew as a
        # curl rather than an arrow. The columns here already say what the
        # number is, so a separator is enough.
        left = "" if pct is None else (
            _compact_reset(lim.resets_at) if lim and not lim.over else "idle")
        out.append((f"{'(' + left + ')':>7}" if left else "       ",
                    _quiet(_reset_tone(lim))))
    return out


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.0f}%"


def _compact_tokens(n: int) -> str:
    """A token count at a glance: 940, 12.3K, 236M, 2.1B."""
    for cutoff, suffix, div in ((1e9, "B", 1e9), (1e6, "M", 1e6), (1e4, "K", 1e3)):
        if n >= cutoff:
            v = n / div
            return f"{v:.0f}{suffix}" if v >= 100 else f"{v:.1f}{suffix}"
    return str(n)


def _context_bar(sess: "sessions.Session") -> list[tuple[str, str]]:
    """How full this session's context window is, labelled so it reads as that.

    Taken from the last request the session made, so it answers the question a
    long conversation actually raises: is this one about to compact?
    """
    pct = sess.context_pct
    if pct is None:
        # A session that has just restarted has not been found in the
        # transcripts yet. Blank space reads as a fault; a dash reads as
        # "not known", which is what it is, and it fills in on the next pass.
        return [("  ctx ", "dim"),
                (("\u2014").center(CTX_BAR_W) + "    ", "dim")]
    filled = max(0, min(CTX_BAR_W, round(pct / 100 * CTX_BAR_W)))
    tone = _quiet(_tone(pct))
    # The unfilled cells were drawn with ░, which at this size reads as static
    # and fights the filled half for attention. The column is a fixed width, so
    # blank space says "the rest" without drawing anything.
    #
    # A healthy bar fills dim rather than in the text colour. Solid black is
    # the heaviest mark on the row, and a bar that says "there is room" should
    # not outweigh one that says "there is not".
    return [("  ctx ", "dim"), (FULL * filled, "dim" if tone == "text" else tone),
            (" " * (CTX_BAR_W - filled), "dim"), (f"{pct:3.0f}%", tone)]


def _spent_cell(sess: "sessions.Session") -> tuple[str, str]:
    """Lifetime tokens for the row. Dim: it is history, not a warning."""
    total = sess.spent.total
    return (f"{_compact_tokens(total) + ' tok' if total else '':>10}", "dim")


def _usage_notes(sess: "sessions.Session") -> list[str]:
    """The numbers behind the row, spelled out.

    The four figures are kept apart because they are not interchangeable: a
    cache read costs a fraction of a fresh input token, and a long conversation
    re-reads its whole context every turn, so cache reads dominate the total
    and a single number would hide what was really spent.
    """
    out = []
    if sess.context_tokens:
        pct = f" ({sess.context_pct:.0f}% full)" if sess.context_pct else ""
        out.append(f"Context now: {sess.context_tokens:,} of {sess.window:,}{pct}")
    if sess.model:
        out.append(f"Model: {sess.model}")
    t = sess.spent
    if t.total:
        out.append(f"Lifetime: {t.total:,} tokens over {t.turns:,} turns")
        out.append(f"    input {t.input:,} · cache write {t.cache_write:,}")
        out.append(f"    cache read {t.cache_read:,} · output {t.output:,}")
    return out


def _known_roots(snap) -> list[str]:
    """Repositories worth offering: the ones sessions are actually in."""
    roots = {core.project_root(s.cwd) for s in snap.sessions if s.cwd}
    return sorted(r for r in roots if r and r != core.HOME)


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _osa_quote(s: str) -> str:
    """A shell command as an AppleScript string literal.

    The command carries quotes of its own, and an unescaped one ends the
    AppleScript string early: the whole call then fails with a syntax error
    that nothing surfaces, so the menu item looks dead.
    """
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run_in_terminal(command: str) -> str:
    """Open a Terminal window running a command. Returns "" or why not."""
    script = (f"tell application \"Terminal\"\n  activate\n"
              f"  do script {_osa_quote(command)}\nend tell")
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)[:120]
    if r.returncode != 0:
        return (r.stderr.strip().splitlines() or ["Terminal refused"])[-1][:160]
    return ""


def _session_segments(sess: "sessions.Session", running_on: str) -> list[tuple[str, str]]:
    """A session row, everything after the focus mark.

    Split out so a row can be repainted with fresh status, context and age
    without rebuilding the menu, which would drop an open menu from under the
    pointer.
    """
    return [
        *_chip(running_on, NAME_W),
        ("  ", "dim"),
        (_fit(sess.repo, REPO_W), "dim"),
        (" ", "dim"),
        (_fit(sess.detail or sess.label, DETAIL_W), "text"),
        *_context_bar(sess),
        ("  " + _fit(sess.status or sess.kind, 5), _quiet(_status_tone(sess.status))),
        (f"{_age(sess.idle_for):>5}", "dim"),
    ]


def _reason(snap: "Snapshot", sess: "sessions.Session") -> str:
    """Which rule decides this session's account, read from the snapshot.

    core.resolve reloads the rules from disk on every call, and the menu asks
    once per session while it draws. The snapshot already holds them.
    """
    return snap.rules.account_for(
        (os.path.abspath(sess.cwd), core.project_root(sess.cwd)), sess.term_id)[1]


def _session_line(sess: "sessions.Session", why: str = "") -> list[tuple[str, str]]:
    """A session, seen from an account or a profile rather than on its own.

    The wide row at the top of the menu answers "what is running". This
    answers "what is running HERE", so it drops the account chip, which is
    the thing the reader already knows by being where they are, and keeps
    what tells one session from another.
    """
    return [
        ("    ", "dim"),
        (_fit(sess.repo, REPO_W), "dim"),
        (" ", "dim"),
        (_fit(sess.detail or sess.label, DETAIL_W), "text"),
        *_context_bar(sess),
        ("  " + _fit(sess.status or sess.kind, 5), _quiet(_status_tone(sess.status))),
        (f"{_age(sess.idle_for):>5}", "dim"),
        (f"   {why}" if why else "", "dim"),
    ]


def _why(reason: str) -> str:
    """Turn a resolution reason into something a person reads."""
    if reason.startswith("profile:"):
        return f"profile “{reason.split(':', 1)[1]}”"
    return {"session": "pinned here", "project": "a project rule",
            "default": "the default"}.get(reason, reason)


def _status_tone(status: str) -> str:
    """Working sessions stand out; idle ones stay quiet.

    Nothing here returns a warning colour. A session sitting at a shell is a
    state, not a problem, and orange now means one thing: a window running out.
    """
    return {"busy": "text"}.get(status, "dim")


def _age(seconds: float) -> str:
    """Compact age: 3m, 5h, 7d. Past two days, hours stop meaning anything."""
    mins = int(seconds // 60)
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    return f"{hours}h" if hours < 48 else f"{hours // 24}d"


def _short(email: Optional[str]) -> str:
    return (email or "?").split("@")[0]


def _shape(snap: "Snapshot") -> tuple:
    """What the menu is made of, as opposed to what it says.

    Percentages, countdowns and ages change on every refresh and are repainted
    in place, so they are deliberately absent here. Only a change in this
    needs rows added or removed, which is the only reason to rebuild.
    """
    r = snap.rules
    return (
        tuple((a.name, a.signed_in, a.error, a.mismatch) for a in snap.accounts),
        tuple(s.pid for s in snap.sessions),
        tuple(sorted(snap.running_on.items())),
        tuple((p.name, p.account, tuple(p.repos)) for p in r.profiles),
        r.default_account,
        tuple(sorted(r.projects.items())),
        tuple(sorted(r.sessions.items())),
    )


def _registry() -> Optional[dict]:
    """rumps' map from NSMenuItem back to the Python object that owns it.

    rumps writes into this on every MenuItem it makes so it can find the
    callback again when AppKit fires, and it never removes anything.
    Menu.clear() empties the NSMenu and rumps' own dict, but this keeps a
    strong reference to every item, its attributed title and its whole
    submenu, so each rebuild leaks the tree it replaced. Measured here at
    about 380 items and several hundred kilobytes per rebuild, with a rebuild
    at least every three minutes.
    """
    try:
        return rumps.rumps.NSApp._ns_to_py_and_callback
    except Exception:
        return None


def _forget(stale: list) -> None:
    """Drop menu items the last build left behind.

    Safe because an item that is in no menu cannot be clicked, so nothing can
    ask for its callback again. Anything still on screen was made after the
    keys were taken and is not in the list.
    """
    reg = _registry()
    if reg is None:
        return
    for key in stale:
        reg.pop(key, None)


_WATCHER: Optional[type] = None


def _watcher_class() -> type:
    """The NSMenu delegate that redraws the menu before it appears.

    Every duration in this menu is computed when the menu is built: how old
    the usage numbers are, when each window resets, how long a session has sat
    idle. Between builds those numbers are frozen, so a row that read
    "updated 59s ago" kept saying it for the next three minutes. Filling a
    menu from its delegate is the supported way to populate one late, so the
    rebuild happens there and every duration is true when it is read.

    Defined on first use, so importing this module does not need AppKit, and
    cached, because an Objective-C class name registers exactly once.
    """
    global _WATCHER
    if _WATCHER is None:
        import AppKit

        class CCMMenuWatcher(AppKit.NSObject):
            def menuWillOpen_(self, _menu):
                try:
                    self.owner._on_menu_open()
                except Exception:
                    pass      # a failed redraw must not stop the menu opening

        _WATCHER = CCMMenuWatcher
    return _WATCHER


class Snapshot:
    def __init__(self) -> None:
        self.accounts: list[core.Account] = []
        self.sessions: list[sessions.Session] = []
        self.rules: core.profiles.Rules = core.profiles.Rules()
        self.running_on: dict[str, str] = {}   # config dir -> account name
        self.taken_at: float = 0.0


class ManagerApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Claude", title=f"{ICON} …", quit_button=None)
        self._snapshot = Snapshot()
        self._pending: Optional[Snapshot] = None
        self._lock = threading.Lock()
        self._busy = False
        self._syncing = False
        self._notice: Optional[tuple[str, bool]] = None
        self._again = False
        self._polling = False
        self._fresh_sessions: Optional[tuple] = None
        self._tracker = focus.Tracker(on_change=self._on_focus_change)
        self._follow_item: Optional[rumps.MenuItem] = None
        self._session_rows: dict[int, tuple[rumps.MenuItem, list]] = {}
        self._account_rows: dict[str, rumps.MenuItem] = {}
        self._refresh_item: Optional[rumps.MenuItem] = None
        self._flash: tuple[str, str, float] = ("", "", 0.0)
        self._done: list = []
        self._drawn_at = 0.0
        self._hide_from_dock()
        self.refresh_now(None)
        self._watch_menu()
        rumps.Timer(self._on_refresh_tick, REFRESH_SECONDS).start()
        rumps.Timer(self._on_credential_tick, CREDENTIAL_SYNC_SECONDS).start()
        rumps.Timer(self._on_sessions_tick, SESSION_POLL_SECONDS).start()
        rumps.Timer(self._on_sync_tick, 1).start()

    # ------------------------------------------------------------------ plumbing

    def _watch_menu(self) -> None:
        """Ask to be told when the menu is about to open. Optional by design.

        Without it the menu still works; its durations are merely as old as
        the last rebuild, which is what they were before.
        """
        try:
            # NSMenu does not retain its delegate, so the app holds it.
            self._watcher = _watcher_class().alloc().init()
            self._watcher.owner = self
            self.menu._menu.setDelegate_(self._watcher)
        except Exception:
            self._watcher = None

    def _style_refresh_row(self, snap: Snapshot) -> None:
        if self._refresh_item is None:
            return
        age = time.time() - snap.taken_at if snap.taken_at else 0.0
        when = "just now" if age < 45 else f"{_age(age)} ago"
        _apply_style(self._refresh_item,
                     [("Refresh now", "text"), (f"   updated {when}", "dim")])

    def _repaint(self) -> None:
        """Redraw the text that changes without the menu changing shape."""
        snap = self._snapshot
        for acct in snap.accounts:
            row = self._account_rows.get(acct.name)
            if row is not None:
                _apply_style(row, self._account_segments(acct, snap))
        self._style_refresh_row(snap)
        self._apply_title(snap)

    def _on_menu_open(self) -> None:
        """Make the durations in the menu true at the moment they are read.

        Only the text that counts against the clock is repainted: when each
        window resets, how old the usage is. Rebuilding the whole menu here
        would be correct too, and it measured about 0.4 s, which reads as a
        stall between the click and the menu. Session rows are left alone
        because the five-second poll already repaints them in place.
        """
        self._repaint()

    @staticmethod
    def _hide_from_dock() -> None:
        """Accessory policy: menu bar only, no Dock icon, no Cmd-Tab entry."""
        try:
            import AppKit
            AppKit.NSApplication.sharedApplication().setActivationPolicy_(
                AppKit.NSApplicationActivationPolicyAccessory)
        except Exception:
            pass

    def _collect(self, force: bool = False) -> Snapshot:
        snap = Snapshot()
        snap.accounts = [core.load_account(n, force=force) for n in core.account_names()]
        snap.sessions = sessions.live(core.credential_dirs(), with_git=True,
                                      with_transcript=True)
        snap.rules = core.bootstrap()
        terms = [s.term_id for s in snap.sessions if s.term_id]
        core.sync_credentials(snap.sessions)
        core.gc_session_dirs(terms)
        snap.running_on = core.dirs_to_accounts(
            {s.env_config_dir for s in snap.sessions}, snap.accounts)
        snap.taken_at = time.time()
        return snap

    def _worker(self, force: bool = False) -> None:
        try:
            snap = self._collect(force)
            with self._lock:
                self._pending = snap
        finally:
            self._busy = False
        if self._again:
            self._again = False
            self._on_refresh_tick(None)

    def _on_refresh_tick(self, _timer, force: bool = False) -> None:
        if self._busy:
            self._again = True     # asked for mid-flight: run again after, not never
            return
        self._busy = True
        threading.Thread(target=self._worker, args=(force,), daemon=True).start()

    def _later(self, fn) -> None:
        """Queue work that has to happen on the main thread. Call from anywhere."""
        with self._lock:
            self._done.append(fn)

    def _on_sync_tick(self, _timer) -> None:
        with self._lock:
            done, self._done = self._done, []
        for fn in done:
            try:
                fn()
            except Exception:
                pass          # one failed follow-up must not stop the rest
        with self._lock:
            notice, self._notice = self._notice, None
        if notice is not None:
            message, refresh = notice
            self._notify(message)
            if refresh:
                self.refresh_now(None)
        with self._lock:
            snap, self._pending = self._pending, None
        if snap is not None:
            changed = _shape(snap) != _shape(self._snapshot)
            self._snapshot = snap
            self._tracker.update_sessions(snap.sessions)
            # Usage numbers move on every refresh and are repainted in place,
            # so a tick that only brings new numbers does not need the menu
            # torn down and built again.
            self._rebuild() if changed else self._repaint()
        self._take_sessions()
        self._tracker.poll()

    def _on_credential_tick(self, _timer) -> None:
        """Keep every copy of each login on the newest credential of its lineage.

        A session has a config dir of its own so it can be switched alone, which
        means several dirs hold copies of one refresh token, and that token is
        single use. Rather than race Claude Code to spend it, this hands the
        newest credential of each lineage to whoever is behind. A session left
        holding a spent one recovers by itself, since it re-reads its keychain
        item about every thirty seconds.

        Keychain work only, no network, so it can run often and off the main
        thread without touching the API budget.
        """
        if self._syncing:
            return
        self._syncing = True

        def work() -> None:
            try:
                core.sync_credentials(self._snapshot.sessions)
            except Exception:
                pass          # a failed pass is retried in under a minute
            finally:
                self._syncing = False

        threading.Thread(target=work, daemon=True).start()

    def _on_sessions_tick(self, _timer) -> None:
        """Notice sessions starting and ending without waiting on the API.

        Reading Claude Code's session files and the transcripts is local and
        costs about a tenth of a second, so it can run every few seconds. Usage
        is the slow part and keeps its own, much longer, interval.
        """
        if self._polling:
            return
        self._polling = True

        def work() -> None:
            try:
                live = sessions.live(core.credential_dirs(), with_git=True,
                                     with_transcript=True)
                # Which account a directory holds only changes when something
                # writes one, so keep the answers already known and look up
                # only directories new since the last pass.
                known = self._snapshot.running_on
                unseen = {s.env_config_dir for s in live} - set(known)
                owners = {**known}
                if unseen:
                    owners.update(core.dirs_to_accounts(unseen, self._snapshot.accounts))
                with self._lock:
                    self._fresh_sessions = (live, owners)
            except Exception:
                pass
            finally:
                self._polling = False

        threading.Thread(target=work, daemon=True).start()

    def _take_sessions(self) -> None:
        """Apply a polled session list, rebuilding only if the set changed."""
        with self._lock:
            fresh, self._fresh_sessions = self._fresh_sessions, None
        if fresh is None:
            return
        live, owners = fresh
        snap = self._snapshot
        structural = ([s.pid for s in live] != [s.pid for s in snap.sessions]
                      or owners != snap.running_on)
        snap.sessions, snap.running_on = live, owners
        self._tracker.update_sessions(live)
        if structural:
            self._rebuild()          # a session came or went: the list must change
            return
        focused = self._tracker.focus.session
        front = focused.pid if focused else None
        for sess in live:            # same rows, fresher numbers: repaint in place
            row = self._session_rows.get(sess.pid)
            if not row:
                continue
            item, _old = row
            segs = _session_segments(sess, owners.get(sess.env_config_dir, ""))
            self._session_rows[sess.pid] = (item, segs)
            mark = (f"{FOCUS_MARK} ", "ok") if sess.pid == front else ("  ", "dim")
            _apply_style(item, [mark] + segs)

    def refresh_now(self, _sender) -> None:
        self._on_refresh_tick(None, force=_sender is not None)

    # ------------------------------------------------------------------ menu

    def _section(self, title: str) -> None:
        """A heading, in the same face as the rows under it.

        These were plain titles, so macOS drew them in the 13pt system font
        while every row below used 12pt monospaced. Two families and two sizes
        in one menu reads as an accident.
        """
        head = rumps.MenuItem(title, callback=None)
        _apply_style(head, [(title, "dim")])
        self.menu.add(head)

    def _rebuild(self) -> None:
        self._drawn_at = time.time()
        snap = self._snapshot
        reg = _registry()
        stale = list(reg) if reg is not None else []
        self._apply_title(snap)
        self.menu.clear()
        self._session_rows = {}
        self._account_rows = {}

        self._follow_item = rumps.MenuItem("Following", callback=None)
        self._style_follow_row(snap)
        self.menu.add(self._follow_item)
        self._add_flash()
        self.menu.add(rumps.separator)

        self._section("SUBSCRIPTIONS")
        for acct in snap.accounts:
            self.menu.add(self._account_item(acct, snap))
        self.menu.add(rumps.separator)

        n = len(snap.sessions)
        self._section(f"RUNNING SESSIONS · {n}" if n else "RUNNING SESSIONS")
        if not snap.sessions:
            self.menu.add(rumps.MenuItem("  none", callback=None))
        for sess in snap.sessions[:14]:
            self.menu.add(self._session_item(sess, snap))
        self.menu.add(rumps.separator)

        self._section("PROFILES")
        for prof in snap.rules.profiles:
            self.menu.add(self._profile_item(prof, snap))
        self.menu.add(self._default_item(snap))
        self.menu.add(rumps.MenuItem("New profile…", callback=self._make_new_profile()))
        self.menu.add(rumps.separator)

        self.menu.add(rumps.MenuItem("Add an account…", callback=self._add_account))
        self.menu.add(rumps.MenuItem(
            "Show the front tab's account" + ("  \u2713" if self._tracker.enabled else ""),
            callback=self._toggle_follow))

        self._refresh_item = rumps.MenuItem("Refresh now", callback=self.refresh_now)
        self._style_refresh_row(snap)
        self.menu.add(self._refresh_item)
        self.menu.add(rumps.MenuItem("Quit", callback=rumps.quit_application))
        _forget(stale)          # the tree this one replaced

    # ------------------------------------------------------------------ title

    def _shown_account(self, snap: Snapshot) -> tuple[Optional[core.Account], Optional[str],
                                                       Optional[sessions.Session]]:
        """The account the menu bar describes: the front tab's, else the default account's."""
        sess = self._tracker.focus.session if self._tracker.enabled else None
        if sess is not None:
            name = snap.running_on.get(sess.env_config_dir, "")
        else:
            name = snap.rules.default_account
        acct = next((a for a in snap.accounts if a.name == name), None)
        return acct, name or None, sess

    @staticmethod
    def _cell(caption: str, lim: "Optional[core.Limit]") -> gauge.Cell:
        """One battery: how much of a window is spent, and when it comes back.

        The countdown is the answer to the question the percentage raises, so
        it belongs beside it rather than one click away. A window that has not
        started has nothing to count down, and says so by staying blank.
        """
        pct = lim.spent if lim else None
        reset = (_compact_reset(lim.resets_at)
                 if lim and lim.resets_at and not lim.over else "")
        return gauge.Cell(caption, pct, _tone(pct), reset, _reset_tone(lim))

    def _apply_title(self, snap: Snapshot) -> None:
        """Replace the text title with the drawn gauge. Falls back to text if AppKit balks."""
        acct, name, sess = self._shown_account(snap)
        name = acct.name if acct else (_short(name) if name else "?")
        if acct:
            fable = next((l for l in acct.limits
                          if l.kind not in ("session", "weekly_all")), None)
            cells = [self._cell("5h", acct.limit("session")),
                     self._cell("7d", acct.limit("weekly_all"))]
            if fable:
                cells.append(self._cell(fable.label, fable))
        else:
            cells = [gauge.Cell("5h", None, "dim"), gauge.Cell("7d", None, "dim")]
        # The tab being followed is named in the menu's first row, not here:
        # the bar is shared with every other app and stays as narrow as it can.
        dim = bool(acct and (acct.error or acct.stale))
        try:
            img = gauge.status_image(name, _chip_color(name), cells, dim=dim)
            item = self._nsapp.nsstatusitem
            item.setTitle_("")
            item.button().setImage_(img)
        except Exception:
            used = " ".join(f"{c.caption} {_pct(c.used)}"
                            + (f" \u21bb{c.reset}" if c.reset else "") for c in cells)
            self.title = f"{ICON} {name} {used}"

    def _style_follow_row(self, snap: Snapshot) -> None:
        """First row: which session the menu bar is describing, and how sure it is."""
        if self._follow_item is None:
            return
        f = self._tracker.focus
        if not self._tracker.enabled:
            segs = [("\u25cb ", "dim"), ("Showing the default account", "text"),
                    ("   following is off", "dim")]
        elif f.session is not None:
            where = f"{f.session.repo} \u00b7 {f.session.detail or f.session.label}"
            segs = [(f"{FOCUS_MARK} ", "ok" if f.exact else "warn"),
                    (_fit(where, 44).rstrip(), "text"),
                    ("   front tab" if f.exact else f"   {f.note}", "dim")]
        else:
            segs = [("\u25cf ", "dim"), ("Showing the default account", "text"),
                    (f"   {f.note or 'nothing to follow yet'}", "dim")]
        _apply_style(self._follow_item, segs)

    def _on_focus_change(self) -> None:
        """Focus moved to another tab: repaint what depends on it, in place.

        The menu is not rebuilt here because it may be open, and rows keep
        their identity so a hover survives. Only the title image, the first
        row and the session markers change.
        """
        snap = self._snapshot
        self._apply_title(snap)
        self._style_follow_row(snap)
        focused = self._tracker.focus.session.pid if self._tracker.focus.session else None
        for pid, (item, segs) in self._session_rows.items():
            mark = (f"{FOCUS_MARK} ", "ok") if pid == focused else ("  ", "dim")
            _apply_style(item, [mark] + segs)

    def _toggle_follow(self, _sender) -> None:
        self._tracker.enabled = not self._tracker.enabled
        self._rebuild()

    @staticmethod
    def _account_segments(acct: core.Account, snap: Snapshot) -> list:
        """One account row. Split out because the countdowns in it age.

        The row is repainted whenever the menu opens, so the reset times and
        the age of the usage are read from the clock at that moment rather
        than from whenever the menu was last built.
        """
        # The dot means "some rule points here", so it counts every scope. The
        # text beside it names only the rules drawn nowhere else: the profiles
        # section below covers both profiles and the default.
        in_use = bool(core.rules_using(acct.name, snap.rules))
        used_by = core.rules_using(acct.name, snap.rules,
                                   scopes=("project", "session"))
        fable = next((l for l in acct.limits
                      if l.kind not in ("session", "weekly_all")), None)
        segments = [
            ("● " if in_use else "○ ", "text" if in_use else "dim"),
            *_chip(acct.name, NAME_W),
        ]
        segments += _bucket("5h", acct.limit("session"))
        segments += _bucket("7d", acct.limit("weekly_all"))
        segments += _bucket(fable.label if fable else "model", fable)
        if used_by:
            segments.append((f"   {', '.join(used_by)}", "dim"))
        if acct.mismatch:
            segments.append((f"   {acct.mismatch}", "hot"))
        elif acct.error:
            segments.append((f"   {acct.error}", "dim"))
        elif acct.stale:
            segments.append((f"   usage from {_age(acct.usage_age)} ago", "dim"))
        return segments

    def _account_item(self, acct: core.Account, snap: Snapshot) -> rumps.MenuItem:
        if not acct.signed_in:
            item = rumps.MenuItem(f"  {acct.name} — {acct.error}")
            _apply_style(item, [(f"  {acct.name:<{NAME_W}}", "text"),
                                (f"  {acct.error}", "hot")])
            again = "Sign in again" if acct.error == "login expired" else "Sign in"
            item.add(self._browser_menu(again, acct.name))
            return item
        in_use = bool(core.rules_using(acct.name, snap.rules))
        # plain title stays unique: rumps keys its callback registry by it
        head = f"{'●' if in_use else '○'} {acct.name} — {_pct(acct.session_pct)} 5h"
        item = rumps.MenuItem(head)
        _apply_style(item, self._account_segments(acct, snap))
        self._account_rows[acct.name] = item

        # The row already carries every bucket, so the submenu is for identity
        # and actions rather than a second copy of the usage.
        detail = rumps.MenuItem(acct.email or "unknown account", callback=None)
        _apply_style(detail, [("  ", "dim"), (acct.email or "unknown account", "text")])
        item.add(detail)
        plan = acct.plan
        if plan:
            plan_item = rumps.MenuItem(plan, callback=None)
            _apply_style(plan_item, [("  ", "dim"), (plan, "dim")])
            item.add(plan_item)
        item.add(rumps.separator)

        self._running_block(
            item, [s for s in snap.sessions
                   if snap.running_on.get(s.env_config_dir) == acct.name],
            "No sessions are running on this account", f"acct:{acct.name}")
        item.add(rumps.separator)

        # An account row is the place to hand it whole groups at once. Inline,
        # for the same reason the scope pickers are: the list is short and a
        # submenu would put it one hover away for nothing.
        head = rumps.MenuItem(f"usehead:{acct.name}", callback=None)
        _apply_style(head, [("  ", "dim"), ("Use this account for", "dim")])
        item.add(head)
        groups = [("default", "", "every project with no rule",
                   snap.rules.default_account == acct.name)]
        groups += [("profile", p.name, f"profile “{p.name}”", p.account == acct.name)
                   for p in snap.rules.profiles]
        for scope, key, label, same in groups:
            row = rumps.MenuItem(f"use:{acct.name}:{scope}:{key}",
                                 callback=None if same else
                                 self._make_assign(scope, key, acct.name, ""))
            _apply_style(row, [("    ", "dim"), ("\u2713 " if same else "  ", "text"),
                               (label, "dim" if same else "text")])
            item.add(row)
        item.add(rumps.separator)

        session = acct.limit("session")
        idle = bool(session and not session.resets_at)
        item.add(rumps.MenuItem(
            "Poke to start the 5h window" if idle else "Poke (window already running)",
            callback=self._make_poke(acct.name)))
        item.add(rumps.separator)

        # Signing in again is always a reasonable thing to want, and when a
        # directory is holding the wrong account it is the only way out — so it
        # cannot live only on rows that already look broken.
        if acct.mismatch:
            note = rumps.MenuItem(f"This is not {acct.name}: {acct.mismatch}", callback=None)
            _apply_style(note, [("  ", "dim"), (f"This is not {acct.name}. "
                                                f"Sign in again to fix it.", "hot")])
            item.add(note)
        item.add(self._browser_menu("Sign in again", acct.name))
        item.add(rumps.MenuItem("Rename…", callback=self._make_rename(acct.name)))
        palette = rumps.MenuItem("Colour")
        current = core.chip_index(acct.name, len(CHIP_COLORS))
        for idx, entry_def in enumerate(CHIP_COLORS):
            label = entry_def[0]
            entry = rumps.MenuItem(f"{label}{'  ✓' if idx == current else ''}",
                                   callback=self._make_recolor(acct.name, idx))
            # show each choice in the colour it would apply
            _apply_style(entry, [(f" {label:<8} ", "chip_fg", f"__palette{idx}"),
                                 ("  ✓" if idx == current else "", "text")])
            palette.add(entry)
        item.add(palette)
        item.add(rumps.MenuItem("Remove account…", callback=self._make_remove(acct.name)))
        return item

    def _session_item(self, sess: sessions.Session, snap: Snapshot) -> rumps.MenuItem:
        """One running session, and the three ways to move it.

        Session, project and profile are the same choice at three widths, so
        they sit in one menu: move this terminal, move the repository, or move
        every repository grouped with it.
        """
        r = snap.rules
        running_on = snap.running_on.get(sess.env_config_dir, "")
        root = core.project_root(sess.cwd)
        wanted, reason = r.account_for(
            (os.path.abspath(sess.cwd), root), sess.term_id)
        ruled = bool(sess.term_id) and sess.term_id in r.sessions
        prof = r.profile_for(root)
        head = f"  {sess.label} — {running_on or '?'} · {sess.status or sess.kind}"
        item = rumps.MenuItem(head)
        # Fixed columns: focus mark, rule mark, chip, repo, what the session
        # is, context bar, lifetime tokens, status, idle age. The repo repeats
        # down the list, so the emphasis goes on the column that tells the
        # rows apart.
        focused = self._tracker.focus.session
        in_front = focused is not None and focused.pid == sess.pid
        segments = _session_segments(sess, running_on)
        _apply_style(item, [(f"{FOCUS_MARK} ", "ok") if in_front else ("  ", "dim")] + segments)
        self._session_rows[sess.pid] = (item, segments)

        if wanted and wanted != running_on:
            # A running session holds its credentials in memory, so the rule
            # cannot reach it. Say what to do rather than only what will happen.
            for line, tone in (
                    (f"Spending {running_on}; {_why(reason)} says {wanted}", "warn"),
                    ("It reads its account once at launch, so restart this tab:", "dim"),
                    ("press ctrl+C twice, then run  claude -c", "dim")):
                d = rumps.MenuItem(line, callback=None)
                _apply_style(d, [("  ", "dim"), (line, tone)])
                item.add(d)
            if focus.bundle_for_program(sess.term_program) and sess.tty:
                item.add(rumps.MenuItem("Take me to that tab",
                                        callback=self._make_reveal(sess)))
            item.add(rumps.separator)   # only when there is something above it
        elif running_on:
            # Which account, and which rule chose it. The row shows the account
            # as a chip; the rule behind it was only ever said when it was
            # being disobeyed, so the answer to "why is this one here" was
            # missing exactly when nothing was wrong.
            line = f"Spending {running_on}, by {_why(reason)}"
            d = rumps.MenuItem(f"why:{sess.pid}", callback=None)
            _apply_style(d, [("  ", "dim"), (line, "dim")])
            item.add(d)
            item.add(rumps.separator)

        if sess.term_id:
            self._add_scope(item, "Use for this session", "session", sess.term_id,
                            snap, current=r.sessions.get(sess.term_id, ""),
                            cwd=sess.cwd, clearable=ruled)
            item.add(rumps.separator)
        self._add_scope(item, f"Use for project “{os.path.basename(root)}”",
                        "project", root, snap,
                        current=r.projects.get(core.profiles.tilde(root), ""),
                        cwd=sess.cwd, clearable=bool(r.project_rule_for(root)))
        if prof:
            item.add(rumps.separator)
            n_proj = len(prof.repos)
            self._add_scope(item, f"Use for profile “{prof.name}” "
                                  f"({n_proj} project{'s' if n_proj != 1 else ''})",
                            "profile", prof.name, snap, current=prof.account,
                            cwd=sess.cwd)
        else:
            join = rumps.MenuItem(f"Add “{os.path.basename(root)}” to profile")
            for p in snap.rules.profiles:
                join.add(rumps.MenuItem(p.name, callback=self._make_join(p.name, root)))
            join.add(rumps.separator)
            join.add(rumps.MenuItem("New profile…", callback=self._make_new_profile(root)))
            item.add(join)
        item.add(rumps.separator)
        item.add(rumps.MenuItem("Open in Finder", callback=self._make_open(sess.cwd)))
        # Reference last. It is worth having and nobody opens this menu to read
        # it, so it sat between the pointer and every action it had to cross.
        item.add(rumps.separator)
        for note in [sess.cwd.replace(core.HOME, "~") or "?"] + _usage_notes(sess):
            note_item = rumps.MenuItem(f"note:{sess.pid}:{note}", callback=None)
            _apply_style(note_item, [("  ", "dim"), (note, "dim")])
            item.add(note_item)
        return item

    def _profile_item(self, prof: "core.profiles.Profile", snap: Snapshot) -> rumps.MenuItem:
        """One profile: the account its repositories use, and which they are."""
        n = len(prof.repos)
        live_here = sum(1 for s in snap.sessions
                        if prof.covers(core.project_root(s.cwd)))
        head = f"  {prof.name} — {prof.account or 'no account'} · {n} repos"
        item = rumps.MenuItem(head)
        _apply_style(item, [
            ("  ", "dim"),
            (_fit(prof.name, PROFILE_W), "text"),
            ("  ", "dim"),
            *(_chip(prof.account, NAME_W) if prof.account
              else [(f"{'unassigned':<{NAME_W}}", "warn")]),
            (f"   {n} project{'s' if n != 1 else ''}", "dim"),
            (f"   {live_here} running" if live_here else "", "dim"),
        ])
        self._add_scope(item, f"Use for every project in “{prof.name}”",
                        "profile", prof.name, snap, current=prof.account, cwd="")
        item.add(rumps.separator)

        self._running_block(
            item, [s for s in snap.sessions if prof.covers(core.project_root(s.cwd))],
            "No sessions are running in these projects", f"prof:{prof.name}")
        item.add(rumps.separator)

        # The projects in the profile are shown, not offered: clicking one had
        # to open a submenu holding a single "remove" item, which is a hover
        # spent on a menu that was never a choice. Removal is its own list.
        if prof.repos:
            head = rumps.MenuItem(f"inhead:{prof.name}", callback=None)
            _apply_style(head, [("  ", "dim"), ("Projects", "dim")])
            item.add(head)
        for repo in prof.repos:
            entry = rumps.MenuItem(f"in:{prof.name}:{repo}", callback=None)
            _apply_style(entry, [("    ", "dim"), (repo, "dim")])
            item.add(entry)
        if not prof.repos:
            empty = rumps.MenuItem(f"empty:{prof.name}", callback=None)
            _apply_style(empty, [("    ", "dim"), ("No projects yet", "dim")])
            item.add(empty)

        spare = [r for r in _known_roots(snap) if not prof.covers(r)]
        if spare:
            add_head = rumps.MenuItem(f"addhead:{prof.name}", callback=None)
            _apply_style(add_head, [("  ", "dim"), ("Add a project", "dim")])
            item.add(add_head)
            for root in spare:
                row = rumps.MenuItem(f"add:{prof.name}:{root}",
                                     callback=self._make_join(prof.name, root))
                _apply_style(row, [("    ", "dim"),
                                   (root.replace(core.HOME, "~"), "text")])
                item.add(row)
        if prof.repos:
            drop = rumps.MenuItem(f"drophead:{prof.name}")
            _apply_style(drop, [("  ", "dim"), ("Remove a project", "text")])
            for repo in prof.repos:
                drop.add(rumps.MenuItem(
                    f"out:{prof.name}:{repo}",
                    callback=self._make_leave(prof.name, core.profiles.expand(repo))))
                _apply_style(drop[f"out:{prof.name}:{repo}"],
                             [(" ", "dim"), (repo, "text")])
            item.add(drop)
        item.add(rumps.separator)
        item.add(rumps.MenuItem("Rename…", callback=self._make_rename_profile(prof.name)))
        item.add(rumps.MenuItem("Remove profile…", callback=self._make_remove_profile(prof.name)))
        return item

    def _default_item(self, snap: Snapshot) -> rumps.MenuItem:
        """Where anything with no rule goes."""
        name = snap.rules.default_account
        loose = sum(1 for s in snap.sessions if _reason(snap, s) == "default")
        item = rumps.MenuItem(f"  everything else — {name or 'not set'}")
        _apply_style(item, [
            ("  ", "dim"),
            (_fit("everything else", PROFILE_W), "dim"),
            ("  ", "dim"),
            *(_chip(name, NAME_W) if name else [(f"{'not set':<{NAME_W}}", "hot")]),
            (f"   {loose} running" if loose else "", "dim"),
        ])
        self._running_block(
            item, [s for s in snap.sessions
                   if _reason(snap, s) == "default"],
            "No sessions are running without a rule", "default")
        item.add(rumps.separator)
        self._add_scope(item, "Use for every project with no rule",
                        "default", "", snap, current=name, cwd="")
        return item

    def _running_block(self, item: rumps.MenuItem, here: list, empty: str,
                       tag: str) -> None:
        """The sessions running on whatever this menu is about.

        A subscription row says how much of a window is gone; a profile row
        says how many projects it covers. Neither says who is spending it,
        which is the question those numbers raise. Each line opens the tab it
        names, so the account at 95% is one click from the terminal burning it.
        """
        head = rumps.MenuItem(f"runhead:{tag}", callback=None)
        _apply_style(head, [("  ", "dim"),
                            (f"Running now · {len(here)}" if here else empty, "dim")])
        item.add(head)
        for sess in here:
            reachable = focus.bundle_for_program(sess.term_program) and sess.tty
            row = rumps.MenuItem(f"run:{tag}:{sess.pid}",
                                 callback=self._make_reveal(sess) if reachable else None)
            _apply_style(row, _session_line(sess))
            item.add(row)

    def _scope_rows(self, title: str, scope: str, key: str, snap: Snapshot,
                    current: str, cwd: str, clearable: bool = False) -> list:
        """An account picker for one scope, as rows to drop straight into a menu.

        These used to be a submenu each, which put the account list one hover
        further away than it needed to be. Every scope offers the same accounts,
        so the scope was the only real choice and it sat in the middle, while
        the constant sat at the end. Naming the scope in a heading and listing
        the accounts under it turns two hovers into one, and lets two scopes be
        read at the same time instead of one at a time.
        """
        head = rumps.MenuItem(title, callback=None)
        _apply_style(head, [("  ", "dim"), (title, "dim")])
        rows = [head]
        for acct in snap.accounts:
            if not acct.signed_in:
                continue
            same = acct.name == current
            # The plain title has to be unique inside one menu, and it is what
            # macOS matches when you type. Scope first, so typing picks a row
            # rather than the first account with that name under any heading.
            entry = rumps.MenuItem(f"{scope}:{key}:{acct.name}",
                                   callback=None if same else
                                   self._make_assign(scope, key, acct.name, cwd))
            _apply_style(entry, [("    ", "dim"),
                                 ("\u2713 " if same else "  ", "text"),
                                 *_chip(acct.name, NAME_W),
                                 (f"  {_pct(acct.session_pct)} 5h", _tone(acct.session_pct))])
            rows.append(entry)
        if clearable:
            drop = rumps.MenuItem(f"clear:{scope}:{key}",
                                  callback=self._make_clear(scope, key, cwd))
            _apply_style(drop, [("    ", "dim"), ("  ", "text"),
                                ("Remove this rule", "text")])
            rows.append(drop)
        return rows

    def _add_scope(self, item: rumps.MenuItem, *args, **kw) -> None:
        for row in self._scope_rows(*args, **kw):
            item.add(row)

    def _make_assign(self, scope: str, key: str, account: str, cwd: str):
        def handler(_sender):
            # A rule change is a local edit and takes about a millisecond. What
            # used to make it feel slow was everything after it: the menu only
            # redrew once a full usage refresh had come back from the API. Draw
            # from what is already known first, then go and check usage.
            applied: dict[str, str] = {}
            ok, msg = core.assign(scope, key, account, cwd=cwd,
                                  live=[], applied_out=applied)
            self._did(ok, msg, applied)
        return handler

    def _add_flash(self) -> None:
        """Show the result of the last rule change, for a short while.

        A rule change used to end in an alert. An alert costs a click, and it
        hides the menu that already shows the answer. This says the same thing
        in the place the user is looking, and goes away on its own.
        """
        text, tone, at = self._flash
        if not text or time.time() - at > FLASH_SECONDS:
            return
        item = rumps.MenuItem(text, callback=None)
        _apply_style(item, [("  ", "dim"), (text, tone)])
        self.menu.add(item)

    def _did(self, ok: bool, message: str, applied: Optional[dict] = None) -> None:
        """Finish a rule change: redraw now, and say what happened in the menu.

        Only the rules moved, so nothing has to come back from the API before
        the menu is right. A failure still needs an alert, because the menu the
        user is about to open would otherwise look exactly as it did before.
        """
        self._flash = (message, "ok" if ok else "hot", time.time())
        if ok:
            self._reflect_rules(applied)      # rebuilds, so the flash appears
            self._apply_later(message)
        else:
            self._rebuild()
            self._notify(message)

    def _report(self, ok: bool, message: str) -> None:
        """Say what happened, in the menu, without calling it a rule change.

        A rebuild rather than a repaint: the flash is a row, so showing one
        changes what the menu is made of.
        """
        self._flash = (message, "ok" if ok else "hot", time.time())
        self._rebuild()

    def _apply_later(self, note: str) -> None:
        """Hand the rules just written to the sessions already running.

        Writing a rule takes about a millisecond. Handing it out does not: the
        account named may hold an expired credential, and refreshing one is two
        requests at a fifteen second timeout each, plus the file locks shared
        with Claude Code. AppKit draws on the thread that would be waiting, so
        doing this inline froze the menu bar item for as long as it took. The
        menu already shows the new rule; this is only the part that reaches
        into running sessions, and it reports back when it lands.
        """
        def work() -> None:
            try:
                moved, applied = core.apply_now(self._snapshot.sessions)
            except Exception:
                return        # the 45 second sync picks the sessions up anyway
            if not applied:
                return
            n = len(moved)
            done = (f"{note}. {n} running session{'s' if n != 1 else ''} "
                    f"switch{'es' if n == 1 else ''} within about 30 seconds"
                    if moved else note)
            self._later(lambda: self._settle(done, applied))

        threading.Thread(target=work, daemon=True).start()

    def _settle(self, message: str, applied: dict) -> None:
        """Report a rule that has reached the sessions it applies to."""
        self._flash = (message, "ok", time.time())
        self._reflect_rules(applied)

    def _reflect_rules(self, applied: Optional[dict] = None) -> None:
        """Redraw immediately from the rules, without waiting on the network.

        Only the directories the change actually wrote to can have moved, and
        the writer already knows what it put in each, so nothing has to be read
        back to draw this.
        """
        snap = self._snapshot
        snap.rules = core.rules()
        snap.running_on = {**snap.running_on, **(applied or {})}
        self._rebuild()

    def _make_reveal(self, sess: sessions.Session):
        def handler(_sender):
            err = focus.reveal_tab(focus.bundle_for_program(sess.term_program), sess.tty)
            if err:
                self._notify(f"Could not open that tab: {err}")
        return handler

    def _make_clear(self, scope: str, key: str, cwd: str):
        def handler(_sender):
            applied: dict[str, str] = {}
            ok, msg = core.clear(scope, key, cwd=cwd,
                                 live=[], applied_out=applied)
            self._did(ok, msg, applied)
        return handler

    def _make_join(self, profile: str, root: str):
        def handler(_sender):
            applied: dict[str, str] = {}
            ok, msg = core.profile_add_repo(profile, root,
                                            live=[], applied_out=applied)
            self._did(ok, msg, applied)
        return handler

    def _make_leave(self, profile: str, root: str):
        def handler(_sender):
            applied: dict[str, str] = {}
            ok, msg = core.profile_remove_repo(profile, root,
                                               live=[], applied_out=applied)
            self._did(ok, msg, applied)
        return handler

    def _make_new_profile(self, root: str = ""):
        def handler(_sender):
            win = rumps.Window(
                title="New profile",
                message="Name a group of repositories that share a subscription.\n"
                        "Example: work, personal, client-acme.",
                ok="Create", cancel="Cancel", dimensions=(240, 22))
            resp = win.run()
            if resp.clicked != 1 or not resp.text.strip():
                return
            name = resp.text.strip()
            applied: dict[str, str] = {}
            ok, msg = core.add_profile(name)
            if ok and root:
                ok, msg = core.profile_add_repo(name, root,
                                                live=[], applied_out=applied)
                msg = f"profile “{name}” created, {msg}" if ok else msg
            self._did(ok, msg, applied)
        return handler

    def _make_rename_profile(self, name: str):
        def handler(_sender):
            win = rumps.Window(title="Rename profile", message=f"New name for “{name}”.",
                               default_text=name, ok="Rename", cancel="Cancel",
                               dimensions=(240, 22))
            resp = win.run()
            if resp.clicked == 1 and resp.text.strip():
                self._did(*core.rename_profile(name, resp.text.strip()))
        return handler

    def _make_remove_profile(self, name: str):
        def handler(_sender):
            if rumps.alert(title=f"Remove “{name}”?",
                           message="Its repositories go back to the default account. "
                                   "No session is disturbed.",
                           ok="Remove", cancel="Cancel") != 1:
                return
            applied: dict[str, str] = {}
            ok, msg = core.remove_profile(name, live=[], applied_out=applied)
            self._did(ok, msg, applied)
        return handler

    def _notify(self, message: str) -> None:
        rumps.alert(title="Claude Code Manager", message=message, ok="OK")

    # ------------------------------------------------------------------ actions

    def _make_poke(self, account: str):
        def handler(_sender):
            # Up to a 45 second request. Never on the drawing thread.
            threading.Thread(target=self._poke, args=(account,), daemon=True).start()
        return handler

    def _poke(self, account: str) -> None:
        ok, msg = core.poke(account)
        self._later(lambda: self._poked(ok, f"{account}: {msg}"))

    def _poked(self, ok: bool, message: str) -> None:
        self._report(ok, message)
        if ok:
            self.refresh_now(None)   # a started window is the point; show it

    def _make_rename(self, account: str):
        def handler(_sender):
            win = rumps.Window(title=f"Rename {account}",
                               message="This renames the account slot and moves its stored\n"
                                       "login with it. Contexts already using it are unaffected.",
                               default_text=account, ok="Rename", cancel="Cancel",
                               dimensions=(240, 22))
            resp = win.run()
            if resp.clicked != 1:
                return
            ok, msg = core.rename_account(account, resp.text)
            rumps.notification("Claude Code Manager", "Renamed" if ok else "Rename failed", msg)
            self.refresh_now(None)
        return handler

    def _make_recolor(self, account: str, index: int):
        def handler(_sender):
            core.set_chip_index(account, index)
            self._rebuild()          # a colour is local; nothing to ask the API
        return handler

    def _make_remove(self, account: str):
        def handler(_sender):
            confirm = rumps.alert(
                title=f"Remove {account}?",
                message="This deletes its stored login from the keychain. Your "
                        "subscription is untouched, and you can add it back with a sign-in.",
                ok="Remove", cancel="Cancel")
            if confirm == 1:
                core.remove_account(account)
                self.refresh_now(None)
        return handler

    def _make_add(self, name: str = ""):
        def handler(_sender):
            self._add_account(None, preset=name)
        return handler

    def _browser_menu(self, title: str, name: str) -> rumps.MenuItem:
        """Pick the browser to sign in with.

        Which browser matters: it signs in as whoever that browser is already
        logged into, and with several subscriptions that is exactly how the
        wrong account gets attached to a name.
        """
        menu = rumps.MenuItem(title)
        for label, app in oauth.installed_browsers():
            menu.add(rumps.MenuItem(label, callback=self._make_sign_in(name, app)))
        return menu

    def _make_sign_in(self, name: str, app: str):
        def handler(_sender):
            self._sign_in(name, app)
        return handler

    def _sign_in(self, name: str, app: str = "") -> None:
        """Sign an account in. The browser hands the code back by itself."""
        try:
            cb = oauth.Callback()
        except OSError:
            self._sign_in_by_paste(name, app)      # cannot listen; ask for the code
            return
        attempt = core.sign_in_begin(name, cb.redirect_uri)
        err = oauth.open_in(attempt.url, app)
        if err:
            cb.close()
            self._notify(f"Could not open a browser: {err}")
            return

        def wait() -> None:
            try:
                if not cb.wait(300):
                    self._post_notice(f"Signing in as “{name}” timed out. Try again.")
                    return
                if cb.error or not cb.code:
                    self._post_notice(f"Sign-in was refused: {cb.error or 'no code came back'}")
                    return
                ok, msg = core.sign_in_finish(attempt, f"{cb.code}#{cb.state}")
                self._post_notice(msg, refresh=ok)
            finally:
                cb.close()

        threading.Thread(target=wait, daemon=True).start()

    def _sign_in_by_paste(self, name: str, app: str) -> None:
        """Fallback for when nothing local can listen: the user pastes the code."""
        attempt = core.sign_in_begin(name)
        err = oauth.open_in(attempt.url, app)
        if err:
            self._notify(f"Could not open a browser: {err}")
            return
        win = rumps.Window(
            title=f"Signing in as “{name}”",
            message="Sign in in the browser, then paste the code it shows here.",
            ok="Sign in", cancel="Cancel", dimensions=(300, 22))
        resp = win.run()
        if resp.clicked != 1 or not resp.text.strip():
            return
        ok, msg = core.sign_in_finish(attempt, resp.text)
        self._notify(msg)
        if ok:
            self.refresh_now(None)

    def _post_notice(self, message: str, refresh: bool = False) -> None:
        """Hand a message to the main thread. AppKit is not thread safe."""
        with self._lock:
            self._notice = (message, refresh)

    def _add_account(self, _sender, preset: str = "") -> None:
        """Open Claude Code in an account's own directory so /login can run.

        Signing in needs a browser round trip that only Claude Code can do, so
        the most this can offer is to put the user in the right place: a fresh
        Terminal already running Claude Code as that account, waiting for
        /login. Names are asked for only when the account is new.
        """
        name = preset
        if not name:
            win = rumps.Window(
                title="Add a Claude account",
                message="Name this account (letters, digits, dashes). It is a label "
                        "for you, not the email.\nA Terminal opens running Claude Code "
                        "as that account, where you type /login.",
                ok="Open Terminal", cancel="Cancel", dimensions=(240, 22))
            resp = win.run()
            if resp.clicked != 1:
                return
            name = resp.text
        name = "".join(ch for ch in name.strip() if ch.isalnum() or ch in "-_")
        if not name:
            return
        self._sign_in(name)   # default browser; per-browser entries live on the row

    @staticmethod
    def _make_open(path: str):
        def handler(_sender):
            subprocess.run(["open", path], check=False)
        return handler


def main() -> None:
    ManagerApp().run()


if __name__ == "__main__":
    main()
