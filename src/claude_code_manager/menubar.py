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

from . import core, focus, gauge, sessions

REFRESH_SECONDS = 180      # usage is not fast-moving; stay light on the API
ICON = "⇄"
FOCUS_MARK = "\u25b8"      # ▸ the session whose tab is in front


# Menu rows are drawn as attributed strings so the three usage buckets line up
# in real columns. A proportional font cannot align with spaces, and a plain
# title cannot colour the bucket that is nearly spent.
BAR_W = 10
NAME_W = 14
REPO_W = 18
DETAIL_W = 30
CTX_BAR_W = 6
PROFILE_W = 18


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


def _chip(name: str, width: int = 0) -> tuple[str, str, str]:
    """A filled rectangle behind the account name, like a terminal badge."""
    label = f" {name} "
    if width:
        label = f" {name.ljust(width)} "
    return (label, "chip_fg", name)


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
    if lim.percent < 85:
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


def _bucket(label: str, lim: Optional[core.Limit], show_reset: bool = True) -> list[tuple[str, str]]:
    pct = lim.percent if lim else None
    tone = _tone(pct)
    if pct is None:
        return [(f"  {label:>5} ", "dim"), (" " * BAR_W, "dim"), ("    —", "dim"),
                *([("      ", "dim")] if show_reset else [])]
    filled = max(0, min(BAR_W, round(pct / 100 * BAR_W)))
    out = [
        (f"  {label:>5} ", "dim"),
        (FULL * filled, tone),
        (EMPTY * (BAR_W - filled), "dim"),
        (f" {pct:3.0f}%", tone),
    ]
    if show_reset:
        out.append((f" \u21bb{_compact_reset(lim.resets_at if lim else None):>4}",
                    _reset_tone(lim)))
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
        return [("  ctx ", "dim"), (" " * (CTX_BAR_W + 4), "dim")]
    filled = max(0, min(CTX_BAR_W, round(pct / 100 * CTX_BAR_W)))
    tone = _tone(pct)
    return [("  ctx ", "dim"), (FULL * filled, tone), (EMPTY * (CTX_BAR_W - filled), "dim"),
            (f"{pct:3.0f}%", tone)]


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


def _why(reason: str) -> str:
    """Turn a resolution reason into something a person reads."""
    if reason.startswith("profile:"):
        return f"profile “{reason.split(':', 1)[1]}”"
    return {"session": "pinned here", "project": "a project rule",
            "default": "the default"}.get(reason, reason)


def _status_tone(status: str) -> str:
    """Working sessions stand out; idle ones stay quiet."""
    return {"busy": "ok", "shell": "warn"}.get(status, "dim")


def _age(seconds: float) -> str:
    """Compact age: 3m, 5h, 7d. Past two days, hours stop meaning anything."""
    mins = int(seconds // 60)
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    return f"{hours}h" if hours < 48 else f"{hours // 24}d"


def _short(email: Optional[str]) -> str:
    return (email or "?").split("@")[0]


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
        self._tracker = focus.Tracker(on_change=self._on_focus_change)
        self._follow_item: Optional[rumps.MenuItem] = None
        self._session_rows: dict[int, tuple[rumps.MenuItem, list]] = {}
        self._hide_from_dock()
        self.refresh_now(None)
        rumps.Timer(self._on_refresh_tick, REFRESH_SECONDS).start()
        rumps.Timer(self._on_sync_tick, 1).start()

    # ------------------------------------------------------------------ plumbing

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
        snap.running_on = {d: core.account_of_dir(d, snap.accounts)
                           for d in {s.env_config_dir for s in snap.sessions}}
        snap.taken_at = time.time()
        return snap

    def _worker(self, force: bool = False) -> None:
        try:
            snap = self._collect(force)
            with self._lock:
                self._pending = snap
        finally:
            self._busy = False

    def _on_refresh_tick(self, _timer, force: bool = False) -> None:
        if self._busy:
            return
        self._busy = True
        threading.Thread(target=self._worker, args=(force,), daemon=True).start()

    def _on_sync_tick(self, _timer) -> None:
        with self._lock:
            snap, self._pending = self._pending, None
        if snap is not None:
            self._snapshot = snap
            self._tracker.update_sessions(snap.sessions)
            self._rebuild()
        self._tracker.poll()

    def refresh_now(self, _sender) -> None:
        self._on_refresh_tick(None, force=_sender is not None)

    # ------------------------------------------------------------------ menu

    def _rebuild(self) -> None:
        snap = self._snapshot
        self._apply_title(snap)
        self.menu.clear()
        self._session_rows = {}

        self._follow_item = rumps.MenuItem("Following", callback=self._toggle_follow)
        self._style_follow_row(snap)
        self.menu.add(self._follow_item)
        self.menu.add(rumps.separator)

        self.menu.add(rumps.MenuItem("SUBSCRIPTIONS", callback=None))
        for acct in snap.accounts:
            self.menu.add(self._account_item(acct, snap))
        self.menu.add(rumps.separator)

        n = len(snap.sessions)
        self.menu.add(rumps.MenuItem(
            f"RUNNING SESSIONS · {n}" if n else "RUNNING SESSIONS", callback=None))
        if not snap.sessions:
            self.menu.add(rumps.MenuItem("  none", callback=None))
        for sess in snap.sessions[:14]:
            self.menu.add(self._session_item(sess, snap))
        self.menu.add(rumps.separator)

        self.menu.add(rumps.MenuItem("PROFILES", callback=None))
        for prof in snap.rules.profiles:
            self.menu.add(self._profile_item(prof, snap))
        self.menu.add(self._default_item(snap))
        self.menu.add(rumps.MenuItem("New profile…", callback=self._make_new_profile()))
        self.menu.add(rumps.separator)

        self.menu.add(rumps.MenuItem("Add an account…", callback=self._add_account))

        age = int(time.time() - snap.taken_at) if snap.taken_at else 0
        self.menu.add(rumps.MenuItem(f"Refresh now (updated {age}s ago)", callback=self.refresh_now))
        self.menu.add(rumps.MenuItem("Quit", callback=rumps.quit_application))

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

    def _apply_title(self, snap: Snapshot) -> None:
        """Replace the text title with the drawn gauge. Falls back to text if AppKit balks."""
        acct, name, sess = self._shown_account(snap)
        name = acct.name if acct else (_short(name) if name else "?")
        if acct:
            fable = next((l for l in acct.limits
                          if l.kind not in ("session", "weekly_all")), None)
            cells = [gauge.Cell("5h", acct.session_pct, _tone(acct.session_pct)),
                     gauge.Cell("7d", acct.weekly_pct, _tone(acct.weekly_pct))]
            if fable:
                cells.append(gauge.Cell(fable.label, fable.percent, _tone(fable.percent)))
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
            used = " ".join(f"{c.caption} {_pct(c.used)}" for c in cells)
            self.title = f"{ICON} {name} {used}"

    def _style_follow_row(self, snap: Snapshot) -> None:
        """First row: what the menu bar is describing and why."""
        if self._follow_item is None:
            return
        f = self._tracker.focus
        if not self._tracker.enabled:
            segs = [("\u25cb ", "dim"), ("Follow the front terminal", "text"),
                    ("   off: showing the default context", "dim")]
        elif f.session is not None:
            where = f"{f.session.repo} \u00b7 {f.session.detail or f.session.label}"
            segs = [(f"{FOCUS_MARK} ", "ok"), (_fit(where, 44).rstrip(), "text")]
            segs.append(("   front tab" if f.exact else "   newest tab, best guess", "dim"))
        else:
            segs = [("\u25cf ", "dim"), ("Follow the front terminal", "text"),
                    (f"   {f.note or 'no session in front'}", "dim")]
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
        self._on_focus_change()

    def _account_item(self, acct: core.Account, snap: Snapshot) -> rumps.MenuItem:
        if not acct.signed_in:
            item = rumps.MenuItem(f"  {acct.name} — {acct.error}")
            _apply_style(item, [(f"  {acct.name:<{NAME_W}}", "text"),
                                (f"  {acct.error}", "hot")])
            item.add(rumps.MenuItem("Sign in…", callback=self._make_add(acct.name)))
            return item
        used_by = core.rules_using(acct.name, snap.rules)
        in_use = bool(used_by)
        # plain title stays unique: rumps keys its callback registry by it
        head = f"{'●' if in_use else '○'} {acct.name} — {_pct(acct.session_pct)} 5h"
        item = rumps.MenuItem(head)

        fable = next((l for l in acct.limits
                      if l.kind not in ("session", "weekly_all")), None)
        segments = [
            ("● " if in_use else "○ ", "text" if in_use else "dim"),
            _chip(acct.name, NAME_W),
        ]
        segments += _bucket("5h", acct.limit("session"))
        segments += _bucket("7d", acct.limit("weekly_all"))
        segments += _bucket(fable.label if fable else "model", fable)
        if used_by:
            segments.append((f"   {', '.join(used_by)}", "dim"))
        if acct.error:
            segments.append((f"   {acct.error}", "dim"))
        elif acct.stale:
            segments.append((f"   {_age(acct.usage_age)} old", "dim"))
        _apply_style(item, segments)

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

        # An account row is the place to hand it whole groups at once.
        use = rumps.MenuItem("Use this account for")
        default_same = snap.rules.default_account == acct.name
        use.add(rumps.MenuItem("Everything with no rule" + ("  ✓" if default_same else ""),
                               callback=None if default_same else
                               self._make_assign("default", "", acct.name, "")))
        for prof in snap.rules.profiles:
            same = prof.account == acct.name
            use.add(rumps.MenuItem(f"Profile “{prof.name}”" + ("  ✓" if same else ""),
                                   callback=None if same else
                                   self._make_assign("profile", prof.name, acct.name, "")))
        item.add(use)
        item.add(rumps.separator)

        session = acct.limit("session")
        idle = bool(session and not session.resets_at)
        item.add(rumps.MenuItem(
            "Poke to start the 5h window" if idle else "Poke (window already running)",
            callback=self._make_poke(acct.name)))
        item.add(rumps.separator)

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
        wanted, reason = core.resolve(sess.cwd, sess.term_id)
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
        segments = [
            ("\u25c9 " if ruled else "  ", "text" if ruled else "dim"),
            _chip(running_on, NAME_W),
            ("  ", "dim"),
            (_fit(sess.repo, REPO_W), "dim"),
            (" ", "dim"),
            (_fit(sess.detail or sess.label, DETAIL_W), "text"),
            *_context_bar(sess),
            _spent_cell(sess),
            (f"  {(sess.status or sess.kind):<6}", _status_tone(sess.status)),
            (f"{_age(sess.idle_for) + ' ago':>9}", "dim"),
        ]
        _apply_style(item, [(f"{FOCUS_MARK} ", "ok") if in_front else ("  ", "dim")] + segments)
        self._session_rows[sess.pid] = (item, segments)

        for note in [sess.cwd.replace(core.HOME, "~") or "?"] + _usage_notes(sess):
            note_item = rumps.MenuItem(note, callback=None)
            _apply_style(note_item, [("  ", "dim"), (note, "dim")])
            item.add(note_item)
        if wanted and wanted != running_on:
            drift = f"On restart it moves to {wanted} ({_why(reason)})"
            d_item = rumps.MenuItem(drift, callback=None)
            _apply_style(d_item, [("  ", "dim"), (drift, "warn")])
            item.add(d_item)
        item.add(rumps.separator)

        if sess.term_id:
            item.add(self._scope_menu(
                "This session only", "session", sess.term_id, snap,
                current=r.sessions.get(sess.term_id, ""), cwd=sess.cwd,
                clearable=ruled))
        item.add(self._scope_menu(
            f"Project “{os.path.basename(root)}”", "project", root, snap,
            current=r.projects.get(core.profiles.tilde(root), ""), cwd=sess.cwd,
            clearable=bool(r.project_rule_for(root))))
        if prof:
            item.add(self._scope_menu(
                f"Profile “{prof.name}” ({len(prof.repos)} repos)", "profile",
                prof.name, snap, current=prof.account, cwd=sess.cwd))
        else:
            join = rumps.MenuItem(f"Add “{os.path.basename(root)}” to profile")
            for p in snap.rules.profiles:
                join.add(rumps.MenuItem(p.name, callback=self._make_join(p.name, root)))
            join.add(rumps.separator)
            join.add(rumps.MenuItem("New profile…", callback=self._make_new_profile(root)))
            item.add(join)
        item.add(rumps.separator)
        item.add(rumps.MenuItem("Open in Finder", callback=self._make_open(sess.cwd)))
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
            _chip(prof.account, NAME_W) if prof.account else (f"{'unassigned':<{NAME_W}}", "warn"),
            (f"   {n} repo{'s' if n != 1 else ''}", "dim"),
            (f"   {live_here} running" if live_here else "", "dim"),
        ])
        item.add(self._scope_menu("Account for every repo here", "profile", prof.name,
                                  snap, current=prof.account, cwd=""))
        item.add(rumps.separator)
        for repo in prof.repos:
            entry = rumps.MenuItem(repo)
            _apply_style(entry, [("  ", "dim"), (repo, "dim")])
            entry.add(rumps.MenuItem("Remove from this profile",
                                     callback=self._make_leave(prof.name, core.profiles.expand(repo))))
            item.add(entry)
        if not prof.repos:
            empty = rumps.MenuItem("No repositories yet", callback=None)
            _apply_style(empty, [("  ", "dim"), ("No repositories yet", "dim")])
            item.add(empty)
        add = rumps.MenuItem("Add a repository")
        for root in _known_roots(snap):
            if prof.covers(root):
                continue
            add.add(rumps.MenuItem(root.replace(core.HOME, "~"),
                                   callback=self._make_join(prof.name, root)))
        item.add(add)
        item.add(rumps.separator)
        item.add(rumps.MenuItem("Rename…", callback=self._make_rename_profile(prof.name)))
        item.add(rumps.MenuItem("Remove profile…", callback=self._make_remove_profile(prof.name)))
        return item

    def _default_item(self, snap: Snapshot) -> rumps.MenuItem:
        """Where anything with no rule goes."""
        name = snap.rules.default_account
        loose = sum(1 for s in snap.sessions
                    if core.resolve(s.cwd, s.term_id)[1] == "default")
        item = rumps.MenuItem(f"  everything else — {name or 'not set'}")
        _apply_style(item, [
            ("  ", "dim"),
            (_fit("everything else", PROFILE_W), "dim"),
            ("  ", "dim"),
            _chip(name, NAME_W) if name else (f"{'not set':<{NAME_W}}", "hot"),
            (f"   {loose} running" if loose else "", "dim"),
        ])
        item.add(self._scope_menu("Account for unruled projects", "default", "",
                                  snap, current=name, cwd=""))
        return item

    def _scope_menu(self, title: str, scope: str, key: str, snap: Snapshot,
                    current: str, cwd: str, clearable: bool = False) -> rumps.MenuItem:
        """An account picker for one scope, ticking whatever it uses now."""
        menu = rumps.MenuItem(title)
        for acct in snap.accounts:
            if not acct.signed_in:
                continue
            same = acct.name == current
            entry = rumps.MenuItem(f"{acct.name} ({_pct(acct.session_pct)} 5h)"
                                   + ("  \u2713" if same else ""),
                                   callback=None if same else
                                   self._make_assign(scope, key, acct.name, cwd))
            _apply_style(entry, [(" ", "dim"), _chip(acct.name, NAME_W),
                                 (f"  {_pct(acct.session_pct)} 5h", _tone(acct.session_pct)),
                                 ("  \u2713" if same else "", "text")])
            menu.add(entry)
        if clearable:
            menu.add(rumps.separator)
            menu.add(rumps.MenuItem("Remove this rule",
                                    callback=self._make_clear(scope, key, cwd)))
        return menu

    def _make_assign(self, scope: str, key: str, account: str, cwd: str):
        def handler(_sender):
            ok, msg = core.assign(scope, key, account, cwd=cwd)
            self._notify(msg, restart=ok)
            self.refresh_now(None)
        return handler

    def _make_clear(self, scope: str, key: str, cwd: str):
        def handler(_sender):
            ok, msg = core.clear(scope, key, cwd=cwd)
            self._notify(msg, restart=ok)
            self.refresh_now(None)
        return handler

    def _make_join(self, profile: str, root: str):
        def handler(_sender):
            ok, msg = core.profile_add_repo(profile, root)
            self._notify(msg, restart=ok)
            self.refresh_now(None)
        return handler

    def _make_leave(self, profile: str, root: str):
        def handler(_sender):
            ok, msg = core.profile_remove_repo(profile, root)
            self._notify(msg, restart=ok)
            self.refresh_now(None)
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
            ok, msg = core.add_profile(resp.text.strip())
            if ok and root:
                core.profile_add_repo(resp.text.strip(), root)
            self._notify(msg, restart=False)
            self.refresh_now(None)
        return handler

    def _make_rename_profile(self, name: str):
        def handler(_sender):
            win = rumps.Window(title="Rename profile", message=f"New name for “{name}”.",
                               default_text=name, ok="Rename", cancel="Cancel",
                               dimensions=(240, 22))
            resp = win.run()
            if resp.clicked == 1 and resp.text.strip():
                self._notify(core.rename_profile(name, resp.text.strip())[1], restart=False)
                self.refresh_now(None)
        return handler

    def _make_remove_profile(self, name: str):
        def handler(_sender):
            if rumps.alert(title=f"Remove “{name}”?",
                           message="Its repositories go back to the default account. "
                                   "No session is disturbed.",
                           ok="Remove", cancel="Cancel") != 1:
                return
            self._notify(core.remove_profile(name)[1], restart=False)
            self.refresh_now(None)
        return handler

    def _notify(self, message: str, restart: bool = False) -> None:
        if restart:
            message += ("\n\nSessions already running keep their account until they "
                        "restart. In that terminal: press ctrl+C twice, then run "
                        "claude -c")
        rumps.alert(title="Claude Code Manager", message=message, ok="OK")

    # ------------------------------------------------------------------ actions

    def _make_poke(self, account: str):
        def handler(_sender):
            ok, msg = core.poke(account)
            rumps.notification("Claude Code Manager",
                               f"Poked {account}" if ok else f"Could not poke {account}", msg)
            self.refresh_now(None)
        return handler

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
            self.refresh_now(None)
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

    def _add_account(self, _sender, preset: str = "") -> None:
        win = rumps.Window(
            title="Add a Claude account",
            message="Name this account slot (letters, digits, dashes).\n"
                    "A Terminal window opens so you can run /login as that account.",
            default_text=preset or "", ok="Open Terminal", cancel="Cancel", dimensions=(240, 22))
        resp = win.run()
        if resp.clicked != 1:
            return
        name = "".join(ch for ch in resp.text.strip() if ch.isalnum() or ch in "-_")
        if not name:
            return
        slot = core.slot_dir(name)
        script = (f'mkdir -p {slot}; echo "Type /login, sign in as {name}, then /exit"; '
                  f'CLAUDE_CONFIG_DIR={slot} claude')
        subprocess.run(["osascript", "-e",
                        f'tell application "Terminal" to do script "{script}"',
                        "-e", 'tell application "Terminal" to activate'], check=False)

    @staticmethod
    def _make_open(path: str):
        def handler(_sender):
            subprocess.run(["open", path], check=False)
        return handler


def main() -> None:
    ManagerApp().run()


if __name__ == "__main__":
    main()
