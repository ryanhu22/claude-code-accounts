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

from . import core, sessions

REFRESH_SECONDS = 180      # usage is not fast-moving; stay light on the API
ICON = "⇄"


# Menu rows are drawn as attributed strings so the three usage buckets line up
# in real columns. A proportional font cannot align with spaces, and a plain
# title cannot colour the bucket that is nearly spent.
BAR_W = 10
NAME_W = 14
REPO_W = 18
DETAIL_W = 30
CTX_BAR_W = 6


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


def _status_tone(status: str) -> str:
    """Working sessions stand out; idle ones stay quiet."""
    return {"busy": "ok", "shell": "warn"}.get(status, "dim")


def _age(seconds: float) -> str:
    mins = int(seconds // 60)
    return f"{mins}m" if mins < 60 else f"{mins // 60}h"


def _short(email: Optional[str]) -> str:
    return (email or "?").split("@")[0]


class Snapshot:
    def __init__(self) -> None:
        self.accounts: list[core.Account] = []
        self.contexts: list[core.Context] = []
        self.context_email: dict[str, str] = {}
        self.sessions: list[sessions.Session] = []
        self.pins: dict[str, str] = {}
        self.taken_at: float = 0.0


class ManagerApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Claude", title=f"{ICON} …", quit_button=None)
        self._snapshot = Snapshot()
        self._pending: Optional[Snapshot] = None
        self._lock = threading.Lock()
        self._busy = False
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
        snap.contexts = core.contexts()
        snap.context_email = core.context_owners([c.path for c in snap.contexts], snap.accounts)
        snap.sessions = sessions.live(core.credential_dirs(), with_git=True,
                                      with_transcript=True)
        snap.pins = core.term_pins()
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
            self._rebuild()

    def refresh_now(self, _sender) -> None:
        self._on_refresh_tick(None, force=_sender is not None)

    # ------------------------------------------------------------------ menu

    def _rebuild(self) -> None:
        snap = self._snapshot
        self.title = self._title_text(snap)
        self.menu.clear()

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

        self.menu.add(rumps.MenuItem("Add an account…", callback=self._add_account))

        age = int(time.time() - snap.taken_at) if snap.taken_at else 0
        self.menu.add(rumps.MenuItem(f"Refresh now (updated {age}s ago)", callback=self.refresh_now))
        self.menu.add(rumps.MenuItem("Quit", callback=rumps.quit_application))

    def _title_text(self, snap: Snapshot) -> str:
        """Title tracks the default context: the account most sessions use."""
        default = next((c for c in snap.contexts if c.name == "default"), None)
        email = snap.context_email.get(default.path) if default else None
        acct = next((a for a in snap.accounts if (a.email or "").lower() == (email or "").lower()), None)
        if not acct:
            return f"{ICON} {_short(email) if email else '?'}"
        return f"{ICON} {_short(acct.email)} {_pct(acct.session_pct)}·{_pct(acct.weekly_pct)}"

    def _account_item(self, acct: core.Account, snap: Snapshot) -> rumps.MenuItem:
        if not acct.signed_in:
            item = rumps.MenuItem(f"  {acct.name} — {acct.error}")
            _apply_style(item, [(f"  {acct.name:<{NAME_W}}", "text"),
                                (f"  {acct.error}", "hot")])
            item.add(rumps.MenuItem("Sign in…", callback=self._make_add(acct.name)))
            return item
        used_by = [c.name for c in snap.contexts
                   if snap.context_email.get(c.path, "").lower() == (acct.email or "").lower()]
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

        for ctx in snap.contexts:
            same = snap.context_email.get(ctx.path, "").lower() == (acct.email or "").lower()
            item.add(rumps.MenuItem(f"Use for “{ctx.name}”" + ("  ✓" if same else ""),
                                    callback=None if same else self._make_swap(acct.name, ctx)))
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
        """One running session, with both ways to move it.

        A login belongs to a config dir, so sessions sharing one always share
        an account. Pinning gives this session a config dir of its own, which
        is the only way to move it without taking its neighbours along.
        """
        email = snap.context_email.get(sess.env_config_dir, "")
        acct_name = next((a.name for a in snap.accounts
                          if (a.email or "").lower() == email.lower()), _short(email))
        pinned = bool(sess.term_id) and sess.term_id in snap.pins
        ctx = core.Context(name=core._ctx_name(sess.env_config_dir),
                           path=sess.env_config_dir)
        head = f"  {sess.label} — {acct_name} · {sess.status or sess.kind}"
        item = rumps.MenuItem(head)
        # Fixed columns: pin mark, chip, repo, what the session is, context
        # bar, status, idle age. The repo repeats down the list, so the
        # emphasis goes on the column that tells the rows apart.
        _apply_style(item, [
            ("\u25c9 " if pinned else "  ", "text" if pinned else "dim"),
            _chip(acct_name, NAME_W),
            ("  ", "dim"),
            (_fit(sess.repo, REPO_W), "dim"),
            (" ", "dim"),
            (_fit(sess.detail or sess.label, DETAIL_W), "text"),
            *_context_bar(sess),
            _spent_cell(sess),
            (f"  {(sess.status or sess.kind):<6}", _status_tone(sess.status)),
            (f"{_age(sess.idle_for):>6}", "dim"),
        ])

        where = rumps.MenuItem(sess.cwd.replace(core.HOME, "~") or "?", callback=None)
        _apply_style(where, [("  ", "dim"), (sess.cwd.replace(core.HOME, "~"), "dim")])
        item.add(where)
        for note in _usage_notes(sess):
            note_item = rumps.MenuItem(note, callback=None)
            _apply_style(note_item, [("  ", "dim"), (note, "dim")])
            item.add(note_item)
        item.add(rumps.separator)

        if sess.term_id:
            item.add(rumps.MenuItem("This session only:", callback=None))
            for acct in snap.accounts:
                if not acct.signed_in:
                    continue
                same = pinned and (acct.email or "").lower() == email.lower()
                item.add(rumps.MenuItem(
                    f"   Pin to {acct.name} ({_pct(acct.session_pct)} 5h)" + ("  \u2713" if same else ""),
                    callback=None if same else self._make_pin(sess, acct.name)))
            if pinned:
                item.add(rumps.MenuItem("   Remove pin", callback=self._make_unpin(sess)))
            item.add(rumps.separator)

        item.add(rumps.MenuItem(f"Every session on “{ctx.name}”:", callback=None))
        for acct in snap.accounts:
            if not acct.signed_in:
                continue
            same = (acct.email or "").lower() == email.lower()
            item.add(rumps.MenuItem(
                f"   {acct.name} ({_pct(acct.session_pct)} 5h)" + ("  \u2713" if same else ""),
                callback=None if same else self._make_swap(acct.name, ctx)))
        item.add(rumps.separator)
        item.add(rumps.MenuItem("Open in Finder", callback=self._make_open(sess.cwd)))
        return item

    def _make_pin(self, sess: sessions.Session, account: str):
        def handler(_sender):
            ok, msg = core.pin(sess.term_id, account, seed_from=sess.env_config_dir)
            self._notify(f"{sess.label}: {msg}" if ok else msg,
                         restart=sess.label if ok else "")
            self.refresh_now(None)
        return handler

    def _make_unpin(self, sess: sessions.Session):
        def handler(_sender):
            ok, msg = core.unpin(sess.term_id)
            self._notify(msg, restart=sess.label if ok else "")
            self.refresh_now(None)
        return handler

    def _notify(self, message: str, restart: str = "") -> None:
        if restart:
            message += (f"\n\n{restart} is already running, so it keeps its current "
                        "account until it restarts. In that terminal: press ctrl+C "
                        "twice, then run claude -c")
        rumps.alert(title="Claude Code Manager", message=message, ok="OK")

    # ------------------------------------------------------------------ actions

    def _make_swap(self, account: str, ctx: core.Context):
        def handler(_sender):
            ok, msg = core.swap(account, ctx)
            rumps.notification("Claude Code Manager", "Swapped" if ok else "Swap failed", msg)
            if ok:
                rumps.alert(title="Account swapped", message=(
                    f"{msg}\n\nNew sessions in “{ctx.name}” use it right away. "
                    "Sessions already running keep their old account until they "
                    "restart: ctrl+C twice, then `claude -c`."), ok="Got it")
            self.refresh_now(None)
        return handler

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
