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
import glob
import json
import os
import subprocess
import threading
import time

import rumps

from . import codex, core, focus, gauge, glyphs, keychain, oauth, sessions

REFRESH_SECONDS = 180      # usage is not fast-moving; stay light on the API
CREDENTIAL_SYNC_SECONDS = 45   # local only: keeps every copy of a login alive
SESSION_POLL_SECONDS = 5       # local only: how soon a new session appears
FLASH_SECONDS = 30             # how long the last rule change stays on screen
ICON = "⇄"
FOCUS_MARK = "\u25b8"      # ▸ the session whose tab is in front
SIGNING_TAIL = ("   signing in\u2026", "warn")   # an account with a browser tab open


# Menu rows are drawn as attributed strings so the three usage buckets line up
# in real columns. A proportional font cannot align with spaces, and a plain
# title cannot colour the bucket that is nearly spent.
NAME_W = 13                # the longest account name, so chips form a column
REPO_W = 12
DETAIL_W = 35
NOTE_LABEL_W = 12          # "cache write" is the widest label in a spec line
NOTE_NUM_W = 13            # and 458,378,917 the widest figure
SUB_DETAIL_W = 30          # the same field inside a submenu, which is narrower
BAR_W = 5                  # a bucket gauge: coarse on purpose, the number is exact
CTX_BAR_W = 6
PROFILE_W = 16
PROJ_W = 12                # "12 projects" is the widest this gets
RUN_W = 11                 # "12 running"
_CODEX_NAMES: set[str] = set()


def _provider_of(name: str) -> str:
    return "codex" if name in _CODEX_NAMES else "claude"


def _fit(text: str, width: int) -> str:
    """Pad or truncate to an exact width so columns cannot be knocked askew."""
    if len(text) <= width:
        return text.ljust(width)
    return text[: max(1, width - 1)] + "\u2026"
# The fill for a bar. A full block (█) is taller than the brackets around it,
# measured: the bracket's ink spans 10 points and the block spans 11.83, so it
# hung below the track it was supposed to sit in. A black square (■) spans 7
# points and its centre lands exactly on the bracket's centre.
FILL = "\u25a0"                            # ■
TICK = "\u00b7"                            # · a graduation, printed whether lit or not


def _colors():
    import AppKit
    return {
        "ok": AppKit.NSColor.systemGreenColor(),
        "warn": AppKit.NSColor.systemOrangeColor(),
        "hot": AppKit.NSColor.systemRedColor(),
        "dim": AppKit.NSColor.secondaryLabelColor(),
        "text": AppKit.NSColor.labelColor(),
        # Time, kept away from the green/orange/red that all mean "how much of
        # an allowance is left". A clock is not a quota.
        "time": AppKit.NSColor.systemBlueColor(),
        # A four step grey ladder, for a column whose value is a magnitude
        # rather than a state. Alpha on labelColor rather than four fixed
        # greys, so it follows the appearance the menu is drawn in. The
        # faintest step stays at 0.38 because a number nobody can read is not
        # a quieter number, it is a missing one.
        "ink1": AppKit.NSColor.labelColor().colorWithAlphaComponent_(0.38),
        "ink2": AppKit.NSColor.labelColor().colorWithAlphaComponent_(0.54),
        "ink3": AppKit.NSColor.labelColor().colorWithAlphaComponent_(0.72),
        "ink4": AppKit.NSColor.labelColor().colorWithAlphaComponent_(0.92),
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


def _chip_colors(chip_key: str):
    """One wash and ink for both the account name and its service mark."""
    import AppKit
    # Terminal-style badge: a faint wash of the hue behind text drawn in
    # that same hue. A solid fill with white text loses badly on the
    # lighter hues, and fails outright in light mode.
    base = _chip_color(chip_key)
    bg = base.colorWithAlphaComponent_(0.22)
    fg = base.blendedColorWithFraction_ofColor_(0.42, AppKit.NSColor.labelColor()) or base
    return bg, fg


def _chip(name: str, width: int = 0, wash: bool = True
          ) -> list[tuple[str, str] | tuple[str, str, str]]:
    """A filled rectangle behind the account name, like a terminal badge.

    The mark rides inside the badge so the account and its service read as
    one token.

    The padding that squares the column sits OUTSIDE the fill. Putting it
    inside made every chip the width of the longest account name, so a short
    name floated in a block of colour and the eye read the block instead of
    the word.
    """
    tone = "chip_fg" if wash else "text"
    key = (name,) if wash else ()
    out = [(" ", tone, *key), (_provider_of(name), "glyph", *key),
           (f"{name} ", tone, *key)]
    if width and len(name) < width:
        out.append((" " * (width - len(name)), "dim"))
    return out


def _tone(pct: float | None) -> str:
    if pct is None:
        return "dim"
    return "ok" if pct < 60 else "warn" if pct < 85 else "hot"


def _icon_run(name: str, size: float = 12.0):
    """One SF Symbol, centred inside a two-cell attachment.

    Squared to the cell so a row that carries an icon keeps every column after
    it in the same place as a row that does not, which is the whole reason
    this is a text attachment and not the menu item's image.
    """
    import AppKit
    font = AppKit.NSFont.monospacedSystemFontOfSize_weight_(size, AppKit.NSFontWeightRegular)
    img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if img is None:
        return AppKit.NSAttributedString.alloc().initWithString_attributes_(
            " ", {AppKit.NSFontAttributeName: font})
    conf = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(
        size - 1.0, AppKit.NSFontWeightRegular)
    img = img.imageWithSymbolConfiguration_(conf) or img
    # Coloured here rather than left as a template. A template takes its colour
    # from the control that draws it, and inside an attachment there is no
    # control to take it from, so it came out black in dark mode. The menu is
    # rebuilt every time it opens, so this is re-read from the appearance often
    # enough to follow a theme change.
    try:
        tinted = img.imageWithSymbolConfiguration_(
            AppKit.NSImageSymbolConfiguration.configurationWithHierarchicalColor_(
                AppKit.NSColor.secondaryLabelColor()))
        img = tinted or img
    except Exception:
        img.setTemplate_(True)
    return _image_run(img, size)


def _glyph_run(provider: str, size: float, tint, background=None):
    """Keep the service mark in the text grid, just like an inline symbol."""
    # Two points smaller than a symbol and set against the left of its box,
    # so the slack of the box falls between the mark and the name. Centred at
    # full size the mark touched the first letter, and a badge whose icon
    # crowds its label reads as one smudge rather than as icon and word.
    img = glyphs.image(provider, size - 2.0, tint)
    return _image_run(img, size, background=background, align="left")


def _image_run(img, size: float, background=None, align: str = "center"):
    """Share the attachment box so provider marks and symbols align."""
    import AppKit
    font = AppKit.NSFont.monospacedSystemFontOfSize_weight_(size, AppKit.NSFontWeightRegular)
    cell = AppKit.NSAttributedString.alloc().initWithString_attributes_(
        "M", {AppKit.NSFontAttributeName: font}).size().width
    # Drawn centred inside a box exactly two cells wide, rather than stretched
    # to fill one. Symbols are not square and they are not all the same shape
    # (a folder is 18 by 14, a dashed square is 15 by 14), so scaling each to
    # a square squashed them by different amounts and left the two rows with
    # icons of visibly different proportions. A fixed box keeps the column,
    # and fitting inside it keeps the shape.
    box_w, box_h = cell * 2, cell * 1.7
    src = img.size()
    scale = min(box_w / src.width, box_h / src.height) if src.width and src.height else 1.0
    if align == "left":
        scale = min(scale, 1.0)   # a mark keeps its size; the slack is the gap
    w, h = src.width * scale, src.height * scale
    boxed = AppKit.NSImage.alloc().initWithSize_(AppKit.NSMakeSize(box_w, box_h))
    boxed.lockFocus()
    img.drawInRect_fromRect_operation_fraction_(
        AppKit.NSMakeRect(0 if align == "left" else (box_w - w) / 2, (box_h - h) / 2, w, h),
        AppKit.NSZeroRect, AppKit.NSCompositingOperationSourceOver, 1.0)
    boxed.unlockFocus()
    att = AppKit.NSTextAttachment.alloc().init()
    att.setImage_(boxed)
    # Dropped below the baseline so the glyph sits on the same optical line as
    # the text beside it rather than riding above it.
    att.setBounds_(AppKit.NSMakeRect(0, -2.5, box_w, box_h))
    out = AppKit.NSAttributedString.attributedStringWithAttachment_(att)
    if background is not None:
        out = AppKit.NSMutableAttributedString.alloc().initWithAttributedString_(out)
        out.addAttribute_value_range_(AppKit.NSBackgroundColorAttributeName,
                                      background, AppKit.NSMakeRange(0, 1))
    return out


_FACES = ("ui", "fig", "mono")


def _face(kind: str, size: float = 12.0):
    """One of three faces, chosen by what a run is.

    ui    what macOS sets every menu in. For words.
    fig   the same face with tabular figures. Digits line up without the text
          around them turning into a fixed pitch grid: every digit is 8.112
          points wide in it, where the plain menu face gives a 1 5.95 and an 8
          8.23, which is why a column of numbers set in it wandered.
    mono  a real fixed pitch face. Only for bars and for tables whose columns
          are words. A block is 11.7 points and a middle dot 3.8 in the system
          face, so a bar drawn there grew longer as it filled.
    """
    import AppKit
    if kind == "mono":
        return AppKit.NSFont.monospacedSystemFontOfSize_weight_(
            size, AppKit.NSFontWeightRegular)
    if kind == "fig":
        # The same size as the fixed pitch face, not a point larger. At 13 the
        # figures carried more weight than the same figures in the
        # subscriptions section, so the identical colour read as a brighter
        # one and two lists of the same numbers looked like two scales.
        return AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(
            size, AppKit.NSFontWeightRegular)
    return AppKit.NSFont.menuFontOfSize_(0)


def _tab_style(tabs, indent: float = 16.0):
    """Columns without a grid: one tab stop per column, aligned as asked.

    This is what lets the words stay words. Padding a label to a character
    width only lines anything up when every glyph is the same size, which was
    the reason this menu was monospaced from end to end.
    """
    import AppKit
    para = AppKit.NSMutableParagraphStyle.alloc().init()
    para.setFirstLineHeadIndent_(indent)
    para.setHeadIndent_(indent)
    para.setLineBreakMode_(AppKit.NSLineBreakByClipping)
    para.setTabStops_([
        AppKit.NSTextTab.alloc().initWithTextAlignment_location_options_(
            AppKit.NSTextAlignmentRight if how == "r" else AppKit.NSTextAlignmentLeft,
            where, {})
        for how, where in tabs])
    return para


def _styled(segments, size: float = 12.0, mono: bool = True, tabs=None):
    """Build an NSAttributedString from runs.

    A run is (text, tone) or (text, tone, chip_key); with a chip key the run is
    drawn on a filled background in that account's colour.

    Two fonts, chosen by what the row is. A row of columns needs every glyph
    the same width or the columns are not columns, so data is monospaced. A
    row you click is a sentence, not a table, and monospacing one makes it look
    like output rather than like a command: this menu had "Rename" and "Remove
    account" set in the same face as a token count. Those use the font macOS
    sets every other menu in.
    """
    import AppKit
    colors = _colors()
    default = "mono" if mono else "ui"
    para = _tab_style(tabs) if tabs else None
    out = AppKit.NSMutableAttributedString.alloc().init()
    for run in segments:
        text, tone = run[0], run[1]
        # A third element names a chip, unless it names one of the faces.
        extra = run[2] if len(run) > 2 else None
        chip_key = None if extra in _FACES else extra
        if tone == "glyph":
            bg, fg = (_chip_colors(chip_key) if chip_key else
                      (None, AppKit.NSColor.secondaryLabelColor()))
            out.appendAttributedString_(_glyph_run(text, size, tint=fg, background=bg))
            continue
        if tone == "icon":
            # An SF Symbol inside the text, not on the menu item. A menu item's
            # image lives in a gutter macOS reserves for a whole run of items,
            # so one of those indented a section heading and every row under
            # it. An attachment is a character: it sits where it is put and
            # moves nothing.
            out.appendAttributedString_(_icon_run(text, size))
            continue
        attrs = {AppKit.NSFontAttributeName:
                 _face(extra if extra in _FACES else default, size)}
        if para is not None:
            attrs[AppKit.NSParagraphStyleAttributeName] = para
        if tone == "head":
            # Letter spacing on a short uppercase label reads as a legend
            # printed on the panel rather than as a row of the data below it.
            attrs[AppKit.NSKernAttributeName] = 1.6
            tone = "dim"
        if chip_key:
            bg, fg = _chip_colors(chip_key)
            attrs[AppKit.NSBackgroundColorAttributeName] = bg
            attrs[AppKit.NSForegroundColorAttributeName] = fg
        else:
            attrs[AppKit.NSForegroundColorAttributeName] = colors.get(tone, colors["text"])
        out.appendAttributedString_(
            AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs))
    return out


_SYMBOLS: dict[str, object] = {}


def _icon(name: str, size: float = 13.0):
    """An SF Symbol for a menu row, as a template image.

    Only the rows that are actions or headings get one. The data rows are
    monospaced columns, and an image at the head of one moves the text off the
    grid that makes them readable as a table.

    Template images take their colour from the menu, so these follow light and
    dark mode and the highlight under the pointer without being told.
    """
    if name in _SYMBOLS:
        return _SYMBOLS[name]
    import AppKit
    img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if img is not None:
        conf = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(
            size, AppKit.NSFontWeightRegular)
        img = img.imageWithSymbolConfiguration_(conf) or img
        img.setTemplate_(True)
    _SYMBOLS[name] = img
    return img


def _set_icon(item: rumps.MenuItem, name: str) -> None:
    img = _icon(name)
    if img is not None:
        item._menuitem.setImage_(img)


# Where a spec line puts its figure and its trailing note. A label set in the
# menu font has no character width to count, so the column is a tab stop rather
# than padding: "cache write" is 70pt and "model" is 37pt, and both have to
# leave the figure ending in the same place.
SPEC_INDENT = 16.0
SPEC_FIGURE_X = 172.0          # right edge of the figures
SPEC_NOTE_X = 180.0            # where anything after them starts


SPEC_TABS = (("r", SPEC_FIGURE_X), ("l", SPEC_NOTE_X))
# The account picker: a chip, then a label and a figure for each of the three
# windows. These base stops shift together when the measured chip width grows
# past 81 points. Three groups of label, figure, countdown allow "fable" 30, "100%" 37,
# "(unused)" 55. The figure stops used to sit where the label ended, leaving
# about a point between "fable" and its number, which is why the pair read as
# one word. Twelve points now, the same gap the subscriptions row leaves.
PICK_TABS = (("l", 108.0), ("r", 172.0), ("l", 180.0),
             ("l", 244.0), ("r", 308.0), ("l", 316.0),
             ("l", 380.0), ("r", 460.0), ("l", 468.0))


def _picker_tabs(chip_width: float) -> tuple[tuple[str, float], ...]:
    """Keep the original gap after the chip without narrowing any column."""
    shift = max(0.0, chip_width - 81.0)
    return tuple((how, where + shift) for how, where in PICK_TABS)


def _live_pids() -> set[int]:
    """Check registry membership without reading environments or transcripts."""
    pids = set()
    for cfg in core.credential_dirs():
        for path in glob.glob(os.path.join(cfg, "sessions", "*.json")):
            try:
                with open(path) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            pid = data.get("pid") if isinstance(data, dict) else None
            if isinstance(pid, int) and sessions.alive(pid):
                pids.add(pid)
    return pids


def _spec_line(label: str, figure: str, tone: str = "text", after=()):
    """A label in the menu face, a figure in the tabular one, one line.

    Nothing here is monospaced except a bar. The words are words, and the
    digits hold their column because the face has tabular figures, not because
    every glyph in the row is the same width.
    """
    runs = [(label, "dim", "ui"), ("\t" + figure, tone, "mono")]
    if after:
        runs.append(("\t", "dim", "ui"))
        runs += [(text, "dim" if kind == "mono" else kind,
                  "mono" if kind == "mono" else "ui") for text, kind in after]
    return _styled(runs, tabs=SPEC_TABS)


def _apply_style(item: rumps.MenuItem, segments, mono: bool = True,
                 tabs=None) -> None:
    """Style a row, falling back silently to its plain title if AppKit balks."""
    try:
        item._menuitem.setAttributedTitle_(_styled(segments, mono=mono, tabs=tabs))
    except Exception:
        pass


def _clock_time(iso: str | None) -> str:
    """When a window comes back, on the wall clock.

    A countdown says how long to wait; a time says whether that lands before
    or after something else you already have planned. They answer different
    questions, so both are here and neither replaces the other.
    """
    if not iso:
        return ""
    try:
        dt = _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    local = dt.astimezone()
    return local.strftime("%-I:%M %p").lower() if local.date() == _dt.datetime.now().date() \
        else local.strftime("%a %-I:%M %p").lower()


def _compact_reset(iso: str | None) -> str:
    """Short countdown for an inline row: 45m, 4h, 34h, 3d.

    Computed from the absolute reset timestamp on every render, so it stays
    right between refreshes instead of ageing with the fetch.
    """
    if not iso:
        # Not a fault and not a stall: this window has no clock because
        # nothing has been spent in it yet. "idle" read as something wrong,
        # and it agrees with the 0% beside it far less than "unused" does.
        return "unused"
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


# How long each window runs. The endpoint sends when a window ends and never
# when it began, so this is what makes "four hours left" readable as a share
# of the window rather than as a bare number.
_WINDOW_SECONDS = {"session": 5 * 3600,
                   "weekly_all": 7 * 86400,
                   "weekly_scoped": 7 * 86400}


def _reset_tone(lim: core.Limit | None) -> str:
    """The countdown's colour: how close this window is to coming back.

    Measured as a share of the window, not as a count of hours. Absolute
    thresholds cannot serve both scales at once: four hours left on a five
    hour window is a window that has barely started, and thirteen hours left
    on a seven day window is one about to roll over, yet an hours-based rule
    called the first one urgent and the second one routine. Exactly backwards.

    So the last tenth of any window is green, whatever that tenth is worth in
    hours. The last third is blue, meaning it is coming and worth planning
    around. Past two thirds remaining the number stops being news and goes
    quiet, because a window that just opened is not something anybody acts on.

    Once a window is nearly spent the wait is what stands between the user and
    work, so it takes the warning colours instead, on real time rather than a
    share, because what matters then is how long you are stopped for: orange
    within the working day, red beyond it. Green still wins in the last tenth,
    since relief that close is the whole answer.
    """
    if lim is None or not lim.resets_at:
        return "dim"           # no clock running, and the word beside it says so
    try:
        dt = _dt.datetime.fromisoformat(str(lim.resets_at).replace("Z", "+00:00"))
    except ValueError:
        return "dim"
    left = (dt - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
    span = lim.span if lim.span > 0 else _WINDOW_SECONDS.get(lim.kind)
    share = (left / span) if span else None
    if share is not None and share <= 0.10:
        return "ok"
    if lim.spent >= 85:
        return "warn" if left < 6 * 3600 else "hot"
    if share is None:
        return "text"
    return "time" if share <= 0.33 else "text" if share <= 0.66 else "dim"


def _gauge(level: float | None, cells: int, tone: str) -> list[tuple[str, str]]:
    """A bar in a bracketed track, filled to `level` per cent.

    Every step of the scale is printed whether it is lit or not, so the bar can
    be read as "three of five" without counting against the bracket. Blank
    space alone gave no way to see how much room was left, and the shade block
    that came before it drew as static at this size.
    """
    if level is None:
        return [("[", "dim"), ("-".center(cells), "dim"), ("]", "dim")]
    filled = max(0, min(cells, round(level / 100 * cells)))
    return [("[", "dim"), (FILL * filled, tone),
            (TICK * (cells - filled), "dim"), ("]", "dim")]


# A session mark is a circle and a quota cell is a square, so the two can never
# be read as each other. Both are the same width in SF Mono.
LIVE, QUIET = "\u25cf", "\u25cb"          # ● ○


def _lamps(here: list, width: int = 4) -> list[tuple[str, str]]:
    """An annunciator for the sessions on this account: lit, and how many.

    Two questions get asked of this column and they want different answers.
    "Is anything working right now" is a state, and a state is read fastest as
    a shape, so it is a filled lamp against a hollow one. "How many are on this
    account" is a quantity, and a quantity is read fastest as a number, which
    also stays exact past the point where marks stop being countable.

    Drawing one mark per session answered the second question badly: four of
    them have to be counted, and ten do not fit. Encoding how long each has
    been idle in the height of its mark answered a third question nobody asked,
    in a shape that needed a legend, and drew the common case as a pair of grey
    hairlines that read as nothing at all.
    """
    if not here:
        return [(" " * width, "dim")]
    working = any((sess.status or "") == "busy" for sess in here)
    # Green for the same reason a busy session row is green: one colour, one
    # meaning, and this lamp is that meaning summed up for the account.
    tone = "ok" if working else "dim"
    return [(LIVE if working else QUIET, tone),
            (f" {len(here)}", tone),
            (" " * max(0, width - 2 - len(str(len(here)))), "dim")]


def _quiet(tone: str) -> str:
    """Let a healthy value be plain text.

    Green marked "nothing is wrong", which is nearly every number, so the menu
    was mostly green and the one figure that mattered had to compete with it.
    Only warn and hot keep a colour, and they now mean one thing. The menu bar
    image keeps its green, because a battery there has no text beside it.
    """
    return "text" if tone == "ok" else tone


def _nothing(_sender) -> None:
    """Does nothing, on purpose.

    Attached to rows that are already true, so macOS leaves them enabled and
    draws them at full strength. Clicking one asks for what it already says.
    """


def _scoped(acct: core.Account) -> core.Limit | None:
    """Show the tighter Codex window, with the short clock winning a tie.

    Claude has one model window. Codex reports a pair, and using the first
    would hide the weekly limit whenever that is the one holding work up.
    """
    limits = [lim for lim in acct.limits if lim.scope]
    if acct.is_codex:
        return max(limits, key=lambda lim: (lim.spent, lim.span == 18000), default=None)
    return next(iter(limits), None)


def _span_label(lim: core.Limit) -> str:
    """Spell out the clock because a model name alone cannot tell the pair apart."""
    if lim.span == 18000:
        return "5h"
    if lim.span == 604800:
        return "7d"
    for span, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if lim.span and lim.span % span == 0:
            return f"{lim.span // span}{unit}"
    return f"{lim.span}s"


def _windows(acct: core.Account) -> list[tuple[str, str, str]]:
    """Every window an account has, as label and figure pairs on tab stops.

    Same three, same order and same colours as the row in the subscriptions
    section, so a reader choosing an account here is comparing the numbers
    they already know rather than a second set that happens to agree.
    """
    out: list[tuple[str, str, str]] = []
    for kind in ("session", "weekly_all"):
        lim = acct.limit(kind)
        out += _window_cell(lim.label if lim else kind, lim)
    scoped = _scoped(acct)
    out += _window_cell(scoped.label if scoped else "fable", scoped)
    return out


def _window_cell(label: str, lim) -> list[tuple[str, str, str]]:
    """One window on a picker row: what it is, what it has spent, when it
    comes back.

    The countdown is here for the same reason the percentage is. Choosing an
    account on a number alone picks the emptiest one, which is the wrong
    choice when that one resets in ten minutes and the fuller one does not
    reset for four days.
    """
    spent = lim.spent if lim else None
    when = ""
    if lim is not None and spent is not None:
        when = _compact_reset(lim.resets_at) if lim.resets_at else "new"
    return [(f"\t{label}", "dim", "ui"),
            (f"\t{_pct(spent)}", _tone(spent), "fig"),
            (f"\t{'(' + when + ')' if when else ''}", _reset_tone(lim), "ui")]


def _bucket(label: str, lim: core.Limit | None, show_reset: bool = True) -> list[tuple[str, str]]:
    """One usage window: its name, how much is used, and when it comes back."""
    # The list fills as the window is spent, so the squares are the usage and
    # the number beside them is the same figure. The menu bar battery is the
    # other way round on purpose: a battery is a level, and a level is what is
    # left, so it drains. Here the question is how much of a window has gone,
    # and five accounts stack up as five bars that grow together.
    spent = lim.spent if lim else None
    tone = _tone(spent)
    # The number takes the same colour as its squares. It was left plain until
    # a window passed 60%, on the argument that green in two places says one
    # thing twice, but the effect was that the exact figure, which is the one
    # a reader checks, was the only part of the instrument with nothing to say
    # about itself. A bar and its number are one reading, so they match.
    # Five cells, not ten. The bar is here to be seen without reading, and the
    # number beside it is the exact figure, so more cells only cost width.
    # The label is right aligned so its padding falls to the left, which puts
    # the whitespace between instruments instead of inside one. Reading a panel
    # depends on each instrument holding together as a unit, and even spacing
    # made the row one long strip of characters.
    # Three spaces before the label, not two, and one after the number rather
    # than five. The countdown used to sit the same distance from its own
    # percentage as from the next window's name, so it read as belonging to
    # whichever one the eye reached first. The row is the same width either
    # way; the space just moved to where it separates instead of joins.
    out = [(f"  {label:>5} ", "dim"), *_gauge(spent, BAR_W, tone),
           # Four wide, so a full window keeps its gap from the track.
           ("    -" if spent is None else f"{spent:4.0f}%", tone)]
    if show_reset:
        # B. A middle dot, not the ↻ used in the menu bar image: SF Mono has no
        # ↻, so it came from a fallback font at a different width and drew as a
        # curl rather than an arrow. The columns here already say what the
        # number is, so a separator is enough.
        when = "" if spent is None else (
            _compact_reset(lim.resets_at) if lim and not lim.over else "unused")
        # Left aligned in a fixed field, so it hugs the number it belongs to
        # and the slack falls on the far side, before the next window.
        out.append((f" {'(' + when + ')':<8}" if when else "         ",
                    _reset_tone(lim)))
    else:
        out.append(("         ", "dim"))
    return out


def _blank_bucket(label: str) -> list[tuple[str, str]]:
    """Keep the slot so the 7d column stays a column across providers.

    The word says the plan has no such window, rather than that nothing is
    known. The blank track includes the two cells used by its brackets.
    """
    return [(f"  {label:>5} ", "dim"), (" " * (BAR_W + 2), "dim"),
            (" none", "dim"), (" " * 9, "dim")]


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v:.0f}%"


def _compact_tokens(n: int) -> str:
    """A token count at a glance: 940, 12.3K, 236M, 2.1B."""
    for cutoff, suffix, div in ((1e9, "B", 1e9), (1e6, "M", 1e6), (1e4, "K", 1e3)):
        if n >= cutoff:
            v = n / div
            return f"{v:.0f}{suffix}" if v >= 100 else f"{v:.1f}{suffix}"
    return str(n)


def _context_bar(sess: sessions.Session, named: bool = True) -> list[tuple[str, str]]:
    """How full this session's context window is, labelled so it reads as that.

    Taken from the last request the session made, so it answers the question a
    long conversation actually raises: is this one about to compact?
    """
    pct = sess.context_pct
    # A session that has just restarted has not been found in the transcripts
    # yet. A dash inside the track reads as "not known", which is what it is,
    # and it fills in on the next pass.
    # Context is not a quota being spent down, it is a buffer filling up, so
    # this one stays the way round it reads: 61% means 61% of the window is in
    # use. Same bracket and same cells, so it is still one family.
    # The name comes off inside a block already headed "Context", where the
    # word was the third time the same thing was said on one line.
    head = ("  ctx " if named else "  ", "dim")
    if pct is None:
        return [head, *_gauge(None, CTX_BAR_W, "dim"), ("    ", "dim")]
    tone = _quiet(_tone(pct))
    return [head, *_gauge(pct, CTX_BAR_W, tone), (f"{pct:3.0f}%", tone)]


# Where one shade of the token ladder ends and the next begins. Lifetime
# totals here run from about two million to about two billion, so the steps
# are decades rather than even splits: on a linear scale every session but the
# heaviest would land in the same band.
_SPEND_STEPS = ((10e6, "ink2"), (100e6, "ink4"), (1e9, "warn"))


def _spent_tone(total: int) -> str:
    """How loud a lifetime total is, by how big it is.

    One grey for every session made this column unreadable as anything but
    text: twelve numbers of equal weight, none of which said which tab has
    been running all week. Four steps of grey were a difference you had to
    look for.

    The ramp is the one the quota bars use, because it measures the same
    thing. A session that has spent two billion tokens and a window that is
    nearly full are both answers to "what is eating the allowance", so they
    are said the same way instead of two ways. Below ten million stays grey:
    that is a tab someone opened, not a tab that is costing anything.
    """
    for cutoff, tone in _SPEND_STEPS:
        if total < cutoff:
            return tone
    return "hot"


def _spent_cell(sess: sessions.Session) -> tuple[str, str]:
    """Lifetime tokens for the row, shaded by how many."""
    total = sess.spent.total
    # Twelve wide, not ten: two of those are the gap that separates this from
    # the context number on its left, which is a different instrument reading
    # a different thing.
    return (f"{_compact_tokens(total) + ' tok' if total else '':>12}",
            _spent_tone(total) if total else "ink1")


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


def _session_segments(sess: sessions.Session, running_on: str) -> list[tuple[str, str]]:
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
        _spent_cell(sess),
        # Four spaces before the status and one after it. The status used to
        # sit two from the token count and four from its own age, so it read
        # as part of the tokens rather than as the state of a session that has
        # been in it for that long. "busy 3m" is one fact.
        ("    " + _fit(sess.status or sess.kind, 5), _status_tone(sess.status)),
        (f"{_age(sess.idle_for):>3}", "dim"),
    ]


def _reason(snap: Snapshot, sess: sessions.Session) -> str:
    """Which rule decides this session's account, read from the snapshot.

    core.resolve reloads the rules from disk on every call, and the menu asks
    once per session while it draws. The snapshot already holds them.
    """
    return snap.rules.account_for(
        (os.path.abspath(sess.cwd), core.project_root(sess.cwd)), sess.term_id)[1]


def _session_line(sess: sessions.Session, why: str = "") -> list[tuple[str, str]]:
    """A session, seen from an account or a profile rather than on its own.

    The wide row at the top of the menu answers "what is running". This
    answers "what is running HERE", so it drops the account chip, which the
    reader already knows by being where they are, and keeps what tells one
    session from another.

    Monospaced, and staying that way. Repository and branch are the two
    columns a reader runs their eye down to tell five sessions apart, and a
    fixed pitch face is what makes a column of words a column.
    """
    return [
        ("    ", "dim"),
        (_fit(sess.repo, REPO_W), "dim"),
        (" ", "dim"),
        (_fit(sess.detail or sess.label, SUB_DETAIL_W), "text"),
        ("  " + _fit(sess.status or sess.kind, 5), _status_tone(sess.status)),
        (f"{_age(sess.idle_for):>3}", "dim"),
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

    Three states, and they were drawn in two. Busy is green, because the
    question this list is opened for is which sessions are working right now.
    A shell is plain text: nothing is running, but somebody is sitting at that
    tab, so it is not the same as idle and it was reading as idle. Idle stays
    grey and recedes.

    Weight rather than a second colour. Green, white, grey is a scale anybody
    reads without a legend, and it spends no hue this menu has already given
    a meaning to.

    Nothing here returns a warning colour. A session at a shell prompt is a
    state, not a problem, and orange still means one thing: a window running
    out.
    """
    return {"busy": "ok", "shell": "text"}.get(status, "dim")


def _age(seconds: float) -> str:
    """Compact age: 3m, 5h, 7d. Past two days, hours stop meaning anything."""
    mins = int(seconds // 60)
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    return f"{hours}h" if hours < 48 else f"{hours // 24}d"


def _short(email: str | None) -> str:
    return (email or "?").split("@")[0]


# What one row costs, measured off a built menu: 774 points over 34 rows and
# 907 over 39, both about 23. A separator is shorter, which only makes the
# estimate cautious, which is the direction to be wrong in.
ROW_HEIGHT = 23.0


def _rows_that_fit() -> int:
    """How many rows the screen has room for before macOS starts scrolling.

    A menu that overflows does not truncate: macOS puts a scroll arrow at each
    end and hides the rest behind them. That is worse than saying what is not
    shown, because an arrow says nothing about how much is behind it and a
    long list is exactly when the reader most wants to know.

    Read from the screen rather than fixed, because the difference between a
    laptop and a desk is the difference between fifteen sessions and sixty.
    """
    import AppKit
    try:
        screen = AppKit.NSScreen.mainScreen()
        bar = AppKit.NSStatusBar.systemStatusBar().thickness()
        # Seventy, not forty. A menu has padding of its own at each end and
        # should not sit flush against the bottom of the screen: at forty a
        # full list came out 1062 points tall against 1055 of room, one row
        # over, which is the whole difference between a list and a list with
        # scroll arrows on it.
        usable = screen.frame().size.height - bar - 70.0
        return max(12, int(usable / ROW_HEIGHT))
    except Exception:
        return 30          # a safe number on the smallest Mac display


def _capped(items: list, room: int) -> tuple[list, int]:
    """The first `room` of a list, and how many were left out.

    The list is in most-recently-active order, so what falls off the end is
    what has sat idle longest, which is the right thing to lose first.
    """
    room = max(1, room)
    return (items, 0) if len(items) <= room else (items[:room], len(items) - room)


def _shape(snap: Snapshot) -> tuple:
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


def _registry() -> dict | None:
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


def _start_timer(callback, seconds: float) -> rumps.Timer:
    """A rumps timer that also runs while a menu is open.

    rumps registers the NSTimer in the default run loop mode only. A menu
    switches the loop into event-tracking mode, where those timers sleep.
    Register the same NSTimer in the common modes, which cover both.
    """
    timer = rumps.Timer(callback, seconds)
    timer.start()
    try:
        from Foundation import NSRunLoop, NSRunLoopCommonModes
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer._nstimer, NSRunLoopCommonModes)
    except (AttributeError, ImportError):
        pass      # older rumps still works, without live menu updates
    return timer


_WATCHER: type | None = None


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

            def menuDidClose_(self, _menu):
                try:
                    self.owner._on_menu_close()
                except Exception:
                    pass      # a failed update must not stop the menu closing

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
        self._pending: Snapshot | None = None
        self._lock = threading.Lock()
        self._busy = False
        self._syncing = False
        # Accounts with a browser tab open for a sign-in, keyed to the attempt
        # that opened it. Main thread only, like every other menu state.
        self._signing_in: dict[str, oauth.Attempt | codex.Attempt] = {}
        # Per config dir, the fingerprint of the credential it held last time
        # it was looked at. What tells a cached owner from a stale one.
        self._owner_prints: dict[str, str | None] = {}
        self._rules_stamp = 0.0
        self._again = False
        self._again_force = False
        self._polling = False
        self._session_poll_lock = threading.Lock()
        self._fresh_sessions: tuple | None = None
        self._tracker = focus.Tracker(on_change=self._on_focus_change)
        self._session_rows: dict[int, tuple[rumps.MenuItem, list]] = {}
        self._sessions_heading = "RUNNING SESSIONS"
        self._pick_tabs = PICK_TABS
        self._account_rows: dict[str, rumps.MenuItem] = {}
        self._refresh_item: rumps.MenuItem | None = None
        self._flash: tuple[str, str, float] = ("", "", 0.0)
        self._done: list = []
        self._menu_open = False
        self._rebuild_pending = False
        self._alerts: list[str] = []
        self._drawn_at = 0.0
        self._hide_from_dock()
        self.refresh_now(None)
        self._watch_menu()
        _start_timer(self._on_refresh_tick, REFRESH_SECONDS)
        _start_timer(self._on_credential_tick, CREDENTIAL_SYNC_SECONDS)
        _start_timer(self._on_sessions_tick, SESSION_POLL_SECONDS)
        _start_timer(self._on_sync_tick, 1)

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
        # The menu face, like every other row in this block. This one row was
        # left monospaced, so the only fixed pitch text below the session list
        # was a command sitting between two commands that were not.
        _apply_style(self._refresh_item,
                     [("Refresh now", "text"), (f"   updated {when}", "dim")],
                     mono=False)

    def _repaint(self) -> None:
        """Redraw the text that changes without the menu changing shape."""
        global _CODEX_NAMES
        snap = self._snapshot
        _CODEX_NAMES = {a.name for a in snap.accounts if a.is_codex}
        for acct in snap.accounts:
            row = self._account_rows.get(acct.name)
            if row is not None:
                _apply_style(row, self._account_segments(acct, snap))
        self._style_refresh_row(snap)
        self._apply_title(snap)

    def _on_menu_open(self) -> None:
        """Make the durations in the menu true at the moment they are read.

        Also the point where the meters start moving, since nothing needs to
        move while nobody is looking.

        A full rebuild costs about 0.4 s, so unchanged registry membership
        keeps the fast path. New or ended sessions need a poll before drawing.
        """
        try:
            if _live_pids() != {s.pid for s in self._snapshot.sessions}:
                self._poll_sessions()
                self._take_sessions()
            if self._rebuild_pending:
                self._rebuild()
        except Exception:
            pass          # a failed poll must still protect the open menu
        self._menu_open = True
        self._repaint()

    def _on_menu_close(self) -> None:
        self._menu_open = False
        if self._rebuild_pending:
            self._rebuild_pending = False
            self._rebuild()
        # Clear first so a modal alert cannot show the queue twice on re-entry.
        alerts, self._alerts = self._alerts, []
        for message in alerts:
            self._notify(message)

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
        snap.accounts = core.all_accounts(force=force)
        snap.sessions = sessions.live(core.credential_dirs(), with_git=True,
                                      with_transcript=True)
        snap.rules = core.bootstrap()
        terms = [s.term_id for s in snap.sessions if s.term_id]
        core.sync_credentials(snap.sessions)
        core.gc_session_dirs(terms)
        core.prune_session_rules(terms)
        snap.running_on = core.dirs_to_accounts(
            {s.env_config_dir for s in snap.sessions}, snap.accounts)
        snap.taken_at = time.time()
        return snap

    def _worker(self, force: bool = False) -> None:
        try:
            snap = self._collect(force)
            try:
                results = core.auto_start(snap.accounts)
                for name, ok, msg in results:
                    message = (f"{name}: weekly windows started automatically" if ok else
                               f"{name}: could not start the weekly window. {msg}")
                    self._later(lambda ok=ok, message=message: self._report(ok, message))
                    if ok:
                        self._again = True
                        self._again_force = True
                        threading.Timer(12.0, lambda name=name: self._later(
                            lambda: self._poke_again(name))).start()
            except Exception:
                pass          # automatic start must not hide a usage reading
            with self._lock:
                self._pending = snap
        finally:
            self._busy = False
        if self._again:
            force = self._again_force
            self._again = False
            self._again_force = False
            self._on_refresh_tick(None, force=force)

    def _on_refresh_tick(self, _timer, force: bool = False) -> None:
        if self._busy:
            self._again = True     # asked for mid-flight: run again after, not never
            self._again_force = self._again_force or force
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
            snap, self._pending = self._pending, None
        if snap is not None:
            changed = _shape(snap) != _shape(self._snapshot)
            added = {s.pid for s in snap.sessions} - {s.pid for s in self._snapshot.sessions}
            self._snapshot = snap
            self._tracker.update_sessions(snap.sessions)
            if self._menu_open and added:
                self._insert_sessions(added)
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
                self._poll_sessions()
            except Exception:
                pass
            finally:
                self._polling = False

        threading.Thread(target=work, daemon=True).start()

    def _poll_sessions(self) -> None:
        # Serialize polls so an older worker cannot overwrite the opening poll.
        with self._session_poll_lock:
            live = sessions.live(core.credential_dirs(), with_git=True, with_transcript=True)
            # A peer can switch credentials or rules. Keep cached owners only
            # while their fingerprints and the rules file stay unchanged.
            known = self._snapshot.running_on
            dirs = {s.env_config_dir for s in live if s.env_config_dir}
            try:
                stamp = os.path.getmtime(core.profiles.CONFIG)
            except OSError:
                stamp = 0.0
            fresh = stamp != self._rules_stamp
            self._rules_stamp = stamp
            if fresh:
                keychain.forget()
            owners, prints = core.owners_now(
                dirs, known, self._owner_prints, self._snapshot.accounts, fresh=fresh)
            self._owner_prints = prints
            with self._lock:
                self._fresh_sessions = (live, owners)

    def _take_sessions(self) -> None:
        """Apply a polled session list, rebuilding only if the set changed."""
        with self._lock:
            fresh, self._fresh_sessions = self._fresh_sessions, None
        if fresh is None:
            return
        live, owners = fresh
        snap = self._snapshot
        added = {s.pid for s in live} - {s.pid for s in snap.sessions}
        structural = ([s.pid for s in live] != [s.pid for s in snap.sessions]
                      or owners != snap.running_on)
        snap.sessions, snap.running_on = live, owners
        self._tracker.update_sessions(live)
        if structural:
            if self._menu_open and added:
                self._insert_sessions(added)
            self._rebuild()          # a session came or went: the list must change
            if not self._menu_open:
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

    def _insert_sessions(self, added: set[int]) -> None:
        """Add rows without replacing the items under the pointer."""
        snap = self._snapshot
        # Keep existing rows until close, even if additions exceed the row cap.
        previous = self._sessions_heading
        for sess in snap.sessions:
            if sess.pid in added and sess.pid not in self._session_rows:
                item = self._session_item(sess, snap)
                self.menu.insert_after(previous, item)
            row = self._session_rows.get(sess.pid)
            if row is not None:
                previous = row[0].title
        if "  none" in self.menu:
            del self.menu["  none"]
        title = f"RUNNING SESSIONS · {len(snap.sessions)}"
        _apply_style(self.menu[self._sessions_heading], [(title, "head")])

    def _section(self, title: str) -> None:
        """A heading, in the same face as the rows under it.

        These were plain titles, so macOS drew them in the 13pt system font
        while every row below used 12pt monospaced. Two families and two sizes
        in one menu reads as an accident.
        """
        head = rumps.MenuItem(title, callback=None)
        _apply_style(head, [(title, "head")])
        self.menu.add(head)

    def _rebuild(self) -> None:
        if self._menu_open:
            # Rows are added and removed here, and a menu redrawn from scratch
            # under the pointer loses the hover and closes any open submenu.
            # Paint what can change in place now; rebuild once it closes.
            # A row click closes the menu before its callback runs, so only
            # timer-driven rebuilds wait. Rule, toggle and colour clicks do not.
            self._rebuild_pending = True
            self._repaint()
            return
        global _CODEX_NAMES
        self._rebuild_pending = False
        snap = self._snapshot
        _CODEX_NAMES = {a.name for a in snap.accounts if a.is_codex}
        chip_width = max((_styled(_chip(a.name), mono=False).size().width
                          for a in snap.accounts if not a.is_codex and a.signed_in), default=0.0)
        self._pick_tabs = _picker_tabs(chip_width)
        self._drawn_at = time.time()
        reg = _registry()
        stale = list(reg) if reg is not None else []
        self._apply_title(snap)
        self.menu.clear()
        self._session_rows = {}
        self._account_rows = {}

        # The row that used to sit here named the session the menu bar was
        # describing and how sure it was of it. Both marks now say that where
        # the thing itself is: the front session is marked in the session
        # list, the account whose numbers are in the menu bar is marked in the
        # subscriptions list, and each mark is green when the answer is certain
        # and orange when it is a guess. So the row was repeating two marks in
        # words, at the top of the menu, which is the most expensive row there
        # is.
        self._add_flash()

        self._section("SUBSCRIPTIONS")
        for acct in snap.accounts:
            self.menu.add(self._account_item(acct, snap))
        self.menu.add(rumps.separator)

        n = len(snap.sessions)
        self._sessions_heading = f"RUNNING SESSIONS · {n}" if n else "RUNNING SESSIONS"
        self._section(self._sessions_heading)
        if not snap.sessions:
            self.menu.add(rumps.MenuItem("  none", callback=None))
        # Everything in this menu that is not a session, counted rather than
        # guessed: a flash row when there is one, three headings, three
        # separators, one row per account, one per profile, the catch-all,
        # and up to six actions. What is left is what the session list may have.
        fixed = 1 + 3 + 3 + len(snap.accounts) + len(snap.rules.profiles) + 1 + 6
        shown, hidden = _capped(snap.sessions, _rows_that_fit() - fixed)
        for sess in shown:
            self.menu.add(self._session_item(sess, snap))
        if hidden:
            self._more(hidden, "main")
        self.menu.add(rumps.separator)

        # Icons from here down, and nowhere above. The two sections above are
        # monospaced columns, and an image at the head of a row pushes its text
        # off the grid that lets them be read as a table. These rows are a list
        # of named things and a list of actions, so a symbol tells them apart
        # faster than reading the first word of each does.
        self._section("PROFILES")
        # No icons on these two. They are a table with the same columns as the
        # two sections above, and macOS pushes a row's text right by the width
        # of its image, so an icon here moved these rows off the left edge
        # every other row in the menu shares. The action rows below have no
        # column to keep, which is why they can afford one.
        for prof in snap.rules.profiles:
            self.menu.add(self._profile_item(prof, snap))
        self.menu.add(self._default_item(snap))
        self.menu.add(rumps.separator)

        # "New profile" sits with the other actions rather than under the
        # profiles it makes. macOS reserves the image gutter for a run of
        # items, so one icon inside the profiles block indented the section
        # heading and the rows with it, and the block no longer began where
        # the two blocks above it began. Everything with an icon is now below
        # the separator, and everything above it is a table.
        new_profile = rumps.MenuItem("New profile…", callback=self._make_new_profile())
        _set_icon(new_profile, "folder.badge.plus")
        self.menu.add(new_profile)
        for provider, label in (("claude", "Claude"), ("codex", "Codex")):
            add = rumps.MenuItem(f"Add a {label} account…",
                                 callback=self._make_add_provider(provider))
            add._menuitem.setImage_(glyphs.template(provider))
            self.menu.add(add)
        follow = rumps.MenuItem("Show the front tab's account",
                                callback=self._toggle_follow)
        _set_icon(follow, "eye")
        # The tick used to be two spaces and a check character on the end of
        # the title, which put it wherever the title happened to stop. A menu
        # item has a state for exactly this, and it draws where macOS draws
        # every other tick in every other menu.
        follow._menuitem.setState_(1 if self._tracker.enabled else 0)
        self.menu.add(follow)
        automatic = rumps.MenuItem("Start weekly windows automatically",
                                   callback=self._toggle_auto_start)
        _set_icon(automatic, "clock.arrow.circlepath")
        automatic._menuitem.setState_(1 if core.pref(core.AUTO_START_PREF, False) else 0)
        self.menu.add(automatic)

        self._refresh_item = rumps.MenuItem("Refresh now", callback=self.refresh_now)
        _set_icon(self._refresh_item, "arrow.clockwise")
        self._style_refresh_row(snap)
        self.menu.add(self._refresh_item)
        quit_item = rumps.MenuItem("Quit", callback=rumps.quit_application)
        _set_icon(quit_item, "power")
        self.menu.add(quit_item)
        _forget(stale)          # the tree this one replaced

    # ------------------------------------------------------------------ title

    def _shown_account(self, snap: Snapshot) -> tuple[core.Account | None, str | None,
                                                       sessions.Session | None]:
        """Show the pinned account, then the front tab's, then the default account."""
        preferred = core.pref("bar_account", "")
        acct = next((a for a in snap.accounts if a.name == preferred), None)
        if acct is not None:
            return acct, acct.name, None
        sess = self._tracker.focus.session if self._tracker.enabled else None
        if sess is not None:
            name = snap.running_on.get(sess.env_config_dir, "")
        else:
            name = snap.rules.default_account
        acct = next((a for a in snap.accounts if a.name == name), None)
        return acct, name or None, sess

    @staticmethod
    def _cell(caption: str, lim: core.Limit | None) -> gauge.Cell:
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
        if acct and acct.reading:
            scoped = _scoped(acct)
            cells = []
            for caption, kind in (("5h", "session"), ("7d", "weekly_all")):
                lim = acct.limit(kind)
                if not acct.is_codex or lim is not None:
                    cells.append(self._cell(caption, lim))
            if scoped:
                cells.append(self._cell(scoped.label, scoped))
        else:
            # A battery reads as what is left, so an empty answer drew as a
            # full one: three hundreds and a confident bar, off no data at all.
            cells = ([gauge.Cell("7d", None, "dim")] if acct and acct.is_codex else
                     [gauge.Cell("5h", None, "dim"), gauge.Cell("7d", None, "dim")])
        # The tab being followed is named in the menu's first row, not here:
        # the bar is shared with every other app and stays as narrow as it can.
        dim = bool(acct and (acct.error or acct.stale or not acct.reading))
        # Keep the fallback independent of AppKit, including chip colours:
        # a failure making a colour should still leave the account readable.
        try:
            section = gauge.Section(acct.provider if acct else "claude", name,
                                    _chip_color(name), cells, dim)
            img = gauge.status_image([section])
            item = self._nsapp.nsstatusitem
            item.setTitle_("")
            item.button().setImage_(img)
        except Exception:
            used = " ".join(f"{c.caption} {_pct(c.used)}"
                            + (f" \u21bb{c.reset}" if c.reset else "") for c in cells)
            self.title = f"{ICON} {name} {used}"

    def _on_focus_change(self) -> None:
        """Focus moved to another tab: repaint what depends on it, in place.

        The menu is not rebuilt here because it may be open, and rows keep
        their identity so a hover survives. Only the title image, the first
        row and the session markers change.
        """
        snap = self._snapshot
        self._apply_title(snap)
        state = self._tracker.focus
        focused = state.session.pid if state.session else None
        for pid, (item, segs) in self._session_rows.items():
            mark = ((f"{FOCUS_MARK} ", "ok" if state.exact else "warn")
                    if pid == focused else ("  ", "dim"))
            _apply_style(item, [mark] + segs)
        # The account rows carry the same mark, so they move with it.
        for acct in snap.accounts:
            row = self._account_rows.get(acct.name)
            if row is not None:
                _apply_style(row, self._account_segments(acct, snap))

    def _toggle_follow(self, _sender) -> None:
        self._tracker.enabled = not self._tracker.enabled
        if self._tracker.enabled:
            core.set_pref("bar_account", "")
        self._rebuild()

    def _toggle_auto_start(self, _sender) -> None:
        enabled = not core.pref(core.AUTO_START_PREF, False)
        core.set_pref(core.AUTO_START_PREF, enabled)
        self._rebuild()
        if enabled:
            self._on_refresh_tick(None, force=True)

    def _make_show_bar(self, name: str):
        def handler(_sender):
            core.set_pref("bar_account", name)
            self._tracker.enabled = False
            self._rebuild()
        return handler

    def _bar_choice(self, item: rumps.MenuItem, acct: core.Account) -> None:
        row = self._line(item, f"bar:{acct.name}", "Show this account in the menu bar",
                         callback=self._make_show_bar(acct.name))
        _set_icon(row, "menubar.rectangle")
        row._menuitem.setState_(1 if core.pref("bar_account", "") == acct.name else 0)

    def _account_segments(self, acct: core.Account, snap: Snapshot) -> list:
        """One account row. Split out because the countdowns in it age.

        The row is repainted whenever the menu opens, so the reset times and
        the age of the usage are read from the clock at that moment rather
        than from whenever the menu was last built.
        """
        # Lamps carry the live state, so the tail is left with the one kind of
        # rule that is drawn nowhere else. A pinned session shows its account
        # on its own row; a profile and the default are in the profiles
        # section; a project rule has no home but this.
        here = [s for s in snap.sessions
                if snap.running_on.get(s.env_config_dir) == acct.name]
        used_by = core.rules_using(acct.name, snap.rules, scopes=("project",))
        # Ahead of the mismatch and the error in the tail: both are what the
        # sign-in is there to fix, and while the browser tab is open the news
        # is that it is being fixed, not what was wrong.
        pending = acct.name in self._signing_in
        scoped = _scoped(acct)
        # The account the menu bar is showing gets the mark the front session
        # gets. The numbers in the menu bar belong to exactly one of these
        # five rows, and until now nothing on the row said which, so the
        # figures up there and the figures down here were two readings a
        # reader had to match by name.
        shown = self._shown_account(snap)[1] == acct.name
        sure = (bool(core.pref("bar_account", ""))
                or not self._tracker.enabled or self._tracker.focus.exact)
        segments = [(f"{FOCUS_MARK} ", "ok" if sure else "warn")
                    if shown else ("  ", "dim"),
                    *_chip(acct.name, NAME_W), ("  ", "dim"), *_lamps(here)]
        if not acct.reading:
            # Nothing usable came back. Every window draws as unknown rather
            # than as empty, because empty is a claim and this is the absence
            # of one, and the note below says the fetch is still trying.
            for label in ("5h", "7d", "model" if acct.is_codex else "fable"):
                segments += _bucket(label, None)
            # Says the app is still trying, because a row of dashes on its own
            # reads as broken rather than as pending.
            segments.append(SIGNING_TAIL if pending
                            else (f"   {acct.error or 'asking again'}", "dim"))
            return segments
        weekly = acct.limit("weekly_all")
        segments += (_blank_bucket("5h")
                     if acct.is_codex and acct.extras.get("has_5h") is False
                     else _bucket("5h", acct.limit("session")))
        segments += _bucket("7d", weekly)
        # The model window usually rolls over with the weekly one, and its
        # countdown was hidden when the two matched to avoid saying the same
        # thing twice. That traded a repeated word for a hole in the row: the
        # only way to read the blank was to know the rule that made it, and
        # scanning a column of resets is easier when every window has one.
        if scoped:
            segments += _bucket(scoped.label, scoped)
        if used_by:
            segments.append((f"   {', '.join(used_by)}", "dim"))
        if pending:
            segments.append(SIGNING_TAIL)
        elif acct.mismatch:
            segments.append((f"   {acct.mismatch}", "hot"))
        elif acct.error:
            segments.append((f"   {acct.error}", "dim"))
        elif acct.stale:
            segments.append((f"   usage from {_age(acct.usage_age)} ago", "dim"))
        return segments

    def _account_item(self, acct: core.Account, snap: Snapshot) -> rumps.MenuItem:
        if not acct.signed_in:
            item = rumps.MenuItem(f"  {acct.name} - {acct.error}")
            _apply_style(item, [("  ", "dim"), *_chip(acct.name, NAME_W, wash=False),
                                SIGNING_TAIL if acct.name in self._signing_in
                                else (f"  {acct.error}", "hot")])
            self._signing_row(item, acct.name)
            again = "Sign in again" if acct.error == "login expired" else "Sign in"
            self._bar_choice(item, acct)
            item.add(self._browser_menu(again, acct.name, provider=acct.provider))
            return item
        # plain title stays unique: rumps keys its callback registry by it
        head = f"{acct.name} - {_pct(acct.session_pct)} 5h"
        item = rumps.MenuItem(head)
        _apply_style(item, self._account_segments(acct, snap))
        self._account_rows[acct.name] = item

        # The row already carries every bucket, so the submenu is for identity
        # and actions rather than a second copy of the usage.
        # The slot name first, in its own colour. This menu opens off a row
        # that is identified by that name and by that colour, and the title
        # named only the email, so the one thing tying the two together was
        # which row the pointer happened to be on. Five accounts and four
        # similar email addresses made that a real question.
        who = rumps.MenuItem(f"who:{acct.name}", callback=None)
        _apply_style(who, [("  ", "dim"), *_chip(acct.name),
                           ("   ", "dim"), (acct.email or "unknown account", "text"),
                           (f"   {acct.plan}" if acct.plan else "", "dim")],
                     mono=False)
        item.add(who)
        item.add(rumps.separator)
        self._usage_block(item, acct)


        if acct.is_codex:
            self._line(item, f"runhead:acct:{acct.name}",
                       "No sessions are listed for Codex accounts yet", tone="dim")
        else:
            self._running_block(
                item, [s for s in snap.sessions
                       if snap.running_on.get(s.env_config_dir) == acct.name],
                "No sessions are running on this account", f"acct:{acct.name}")
        item.add(rumps.separator)

        # There is no "use this account for" block here. It listed the profiles
        # and the default and let you point them at this account, which is the
        # profiles section read backwards. One place to set a rule is enough,
        # and the profiles section is the one that shows what every rule is.

        # Only offered when it can do something. A window starts on the first
        # request and this sends one, so an account nobody has used yet has its
        # clocks stopped. Once they run there is nothing to start, and the row
        # said so, which the account row above already shows and is not a thing
        # to click.
        #
        # It used to test the five hour window alone and name it in the label,
        # so an account whose only stopped clock was the Fable one showed no
        # button at all, and the row it would have fixed kept reading "unused".
        stopped = [lim for lim in acct.limits if not lim.resets_at]
        if not acct.is_codex and acct.reading and stopped:
            names = ", ".join(lim.label for lim in stopped)
            _set_icon(self._line(item, f"poke:{acct.name}",
                                 f"Start the {names} window now" if len(stopped) == 1
                                 else f"Start the {names} windows now",
                                 callback=self._make_poke(acct.name)), "clock.arrow.circlepath")
            item.add(rumps.separator)

        # Signing in again is always a reasonable thing to want, and when a
        # directory is holding the wrong account it is the only way out - so it
        # cannot live only on rows that already look broken.
        if acct.mismatch:
            note = rumps.MenuItem(f"This is not {acct.name}: {acct.mismatch}", callback=None)
            _apply_style(note, [("  ", "dim"), (f"This is not {acct.name}. "
                                                f"Sign in again to fix it.", "hot")],
                         mono=False)
            _set_icon(note, "exclamationmark.triangle")
            item.add(note)
        self._signing_row(item, acct.name)
        # Icons on every row of this run and on none above it. macOS reserves
        # the image gutter for a whole run, so a run that is part icons and
        # part not indents itself unevenly, and the session table above must
        # keep the left edge it shares with the menu it opened from.
        self._bar_choice(item, acct)
        signin = self._browser_menu("Sign in again", acct.name, provider=acct.provider)
        _apply_style(signin, [("  ", "dim"), ("Sign in again", "text")], mono=False)
        _set_icon(signin, "person.crop.circle.badge.checkmark")
        item.add(signin)
        _set_icon(self._line(item, f"rename:{acct.name}", "Rename\u2026",
                             callback=self._make_rename(acct.name)), "pencil")
        palette = rumps.MenuItem(f"colour:{acct.name}")
        _apply_style(palette, [("  ", "dim"), ("Colour", "text")], mono=False)
        _set_icon(palette, "paintpalette")
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
        _set_icon(self._line(item, f"rm:{acct.name}", "Remove account\u2026",
                             callback=self._make_remove(acct.name)), "trash")
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
        # rumps keys rows by title, and two sessions can share a label and status.
        head = f"  {sess.label} - {running_on or '?'} · {sess.status or sess.kind} · {sess.pid}"
        item = rumps.MenuItem(head)
        # Fixed columns: focus mark, rule mark, chip, repo, what the session
        # is, context bar, lifetime tokens, status, idle age. The repo repeats
        # down the list, so the emphasis goes on the column that tells the
        # rows apart.
        # Green when this really is the front tab, orange when it is the app's
        # best guess. The row at the top of the menu used to be the only place
        # that difference was said, in words. It is the same fact either way,
        # and the mark is already here.
        focus_state = self._tracker.focus
        focused = focus_state.session
        in_front = focused is not None and focused.pid == sess.pid
        segments = _session_segments(sess, running_on)
        _apply_style(item, [(f"{FOCUS_MARK} ", "ok" if focus_state.exact else "warn")
                            if in_front else ("  ", "dim")] + segments)
        self._session_rows[sess.pid] = (item, segments)

        if wanted and wanted != running_on:
            # A running session holds its credentials in memory, so the rule
            # cannot reach it. Say what to do rather than only what will happen.
            for line, tone in (
                    (f"Spending {running_on}; {_why(reason)} says {wanted}", "warn"),
                    ("It reads its account once at launch, so restart this tab:", "dim"),
                    ("press ctrl+C twice, then run  claude -c", "dim")):
                d = rumps.MenuItem(line, callback=None)
                _apply_style(d, [("  ", "dim"), (line, tone)], mono=False)
                item.add(d)
            if focus.bundle_for_program(sess.term_program) and sess.tty:
                _set_icon(self._line(item, f"tab:{sess.pid}", "Take me to that tab",
                                     callback=self._make_reveal(sess)),
                          "arrow.up.forward.app")
            # The account it is actually spending, not the one the rule
            # names. Until the tab restarts, that is the one being drawn down.
            spending = next((a for a in snap.accounts if a.name == running_on), None)
            if spending is not None and spending.reading:
                self._usage_block(item, spending)
            else:
                item.add(rumps.separator)   # only when there is something above it
        elif running_on:
            # Which account, and which rule chose it. The row shows the account
            # as a chip; the rule behind it was only ever said when it was
            # being disobeyed, so the answer to "why is this one here" was
            # missing exactly when nothing was wrong.
            line = f"Spending {running_on}, by {_why(reason)}"
            d = rumps.MenuItem(f"why:{sess.pid}", callback=None)
            _apply_style(d, [("  ", "dim"), (line, "dim")], mono=False)
            item.add(d)
            # How the account named on the line above is doing. That line
            # says which subscription this session spends, and the next
            # question it raises is how much of that subscription is left.
            # The answer used to be in another section of the menu, which
            # meant closing this one to go and read it.
            spending = next((a for a in snap.accounts if a.name == running_on), None)
            if spending is not None and spending.reading:
                self._usage_block(item, spending)
            else:
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
        join = None
        if prof:
            # One line, not another five. The profiles section edits profiles
            # and is one hover away, so repeating its account picker here cost
            # a third of the menu's height for the least likely action in it.
            n_proj = len(prof.repos)
            note = (f"In profile “{prof.name}” ({n_proj} project"
                    f"{'s' if n_proj != 1 else ''}), which uses "
                    f"{prof.account or 'no account'}")
            row = rumps.MenuItem(f"prof:{sess.pid}", callback=None)
            _apply_style(row, [("  ", "dim"), (note, "dim")], mono=False)
            item.add(row)
        else:
            join = rumps.MenuItem(f"Add “{os.path.basename(root)}” to profile")
            _set_icon(join, "folder.badge.plus")
            for p in snap.rules.profiles:
                entry = rumps.MenuItem(p.name, callback=self._make_join(p.name, root))
                # The same folder the PROFILES section marks a profile with. A
                # profile is a profile wherever it is listed, and this was the
                # one list that had it as a bare word.
                _apply_style(entry, [(" ", "dim"), ("folder", "icon"),
                                     (" ", "dim"), (p.name, "text")], mono=False)
                join.add(entry)
            join.add(rumps.separator)
            fresh = rumps.MenuItem(f"newp:{sess.pid}",
                                  callback=self._make_new_profile(root))
            _apply_style(fresh, [(" ", "dim"), ("folder.badge.plus", "icon"),
                                 (" ", "dim"), ("New profile\u2026", "text")],
                         mono=False)
            join.add(fresh)
        item.add(rumps.separator)
        # Below the separator, with the other icons. macOS reserves the
        # image gutter for a run of items rather than for the one item
        # that has an image, so above the separator this row pushed the
        # whole project picker two columns right of the session picker.
        if join is not None:
            item.add(join)
        # The same block a session gets when it is reached through its
        # account, so the numbers behind a row read the same way whichever
        # menu was opened to find them. They were six ragged rows here once,
        # cut down to a tooltip, which put them somewhere nothing else in this
        # app keeps anything and made them wait on a hover timer.
        detail = rumps.MenuItem(f"detail:{sess.pid}")
        _apply_style(detail, [("  ", "dim"), ("Context and spend", "text")], mono=False)
        _set_icon(detail, "chart.bar")
        self._session_notes(detail, sess, reachable=False, tag=f"row{sess.pid}")
        item.add(detail)
        _set_icon(self._line(item, f"finder:{sess.pid}", "Open in Finder",
                             callback=self._make_open(sess.cwd)), "folder")
        # The path names the session, and it is the one string here that is
        # not a sentence, so it keeps the monospaced face.
        where = sess.cwd.replace(core.HOME, "~") or "?"
        note_item = rumps.MenuItem(f"note:{sess.pid}", callback=None)
        _apply_style(note_item, [("  ", "dim"), (_fit(where, 60).rstrip(), "dim")],
                     mono=False)
        try:
            note_item._menuitem.setToolTip_(where)
        except Exception:
            pass
        item.add(note_item)
        return item

    def _profile_item(self, prof: core.profiles.Profile, snap: Snapshot) -> rumps.MenuItem:
        """One profile: the account its repositories use, and which they are."""
        n = len(prof.repos)
        here = [s for s in snap.sessions
                if prof.covers(core.project_root(s.cwd))]
        head = f"  {prof.name} - {prof.account or 'no account'} · {n} repos"
        item = rumps.MenuItem(head)
        # The account first, as in every other row in this menu. A subscription
        # row leads with the account it is, a session row with the account it
        # spends, and a profile row with the account it routes to. One column
        # one meaning, and all three sections line up down the left because of
        # it. Reading the profile name first was the truer order for this
        # section alone, and it cost the menu its grid.
        _apply_style(item, [
            ("  ", "dim"),
            *(_chip(prof.account, NAME_W) if prof.account
              else [(f"{'unassigned':<{NAME_W + 2}}", "warn")]),
            # The same lamp a subscription row uses, but after the profile
            # name rather than after the account. These sessions are running
            # because of this profile's rule, not because of the account it
            # points at, and next to the chip it read as the account's count,
            # which is the number one row up in the section above.
            ("  ", "dim"), ("folder", "icon"), (" ", "dim"),
            (_fit(prof.name, PROFILE_W), "text"),
            ("  ", "dim"), *_lamps(here),
            (f"  {str(n) + ' project' + ('s' if n != 1 else ''):<{PROJ_W}}", "dim"),
        ])
        self._add_scope(item, f"Use for every project in “{prof.name}”",
                        "profile", prof.name, snap, current=prof.account, cwd="")
        item.add(rumps.separator)

        self._running_block(
            item, here,
            "No sessions are running in these projects", f"prof:{prof.name}")
        item.add(rumps.separator)

        # The projects in the profile are shown, not offered: clicking one had
        # to open a submenu holding a single "remove" item, which is a hover
        # spent on a menu that was never a choice. Removal is its own list.
        if prof.repos:
            self._legend(item, f"in:{prof.name}", "Projects")
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
            self._legend(item, f"add:{prof.name}", "Add a project")
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
        self._line(item, f"pren:{prof.name}", "Rename\u2026",
                   callback=self._make_rename_profile(prof.name))
        self._line(item, f"prm:{prof.name}", "Remove profile\u2026",
                   callback=self._make_remove_profile(prof.name))
        return item

    def _default_item(self, snap: Snapshot) -> rumps.MenuItem:
        """Where anything with no rule goes."""
        name = snap.rules.default_account
        loose = [s for s in snap.sessions if _reason(snap, s) == "default"]
        item = rumps.MenuItem(f"  everything else - {name or 'not set'}")
        _apply_style(item, [
            ("  ", "dim"),
            *(_chip(name, NAME_W) if name else [(f"{'not set':<{NAME_W + 2}}", "hot")]),
            # Dashed, because this is not a profile anybody made. It is
            # what collects whatever the named ones did not.
            ("  ", "dim"), ("square.dashed", "icon"), (" ", "dim"),
            (_fit("everything else", PROFILE_W), "dim"),
            ("  ", "dim"), *_lamps(loose),
            # No project count. This rule covers whatever is not in a profile,
            # so it has nothing to count, and a number here would sit under
            # the row above meaning something else.
            (" " * (2 + PROJ_W), "dim"),
        ])
        self._running_block(
            item, [s for s in snap.sessions
                   if _reason(snap, s) == "default"],
            "No sessions are running without a rule", "default")
        item.add(rumps.separator)
        self._add_scope(item, "Use for every project with no rule",
                        "default", "", snap, current=name, cwd="")
        return item

    @staticmethod
    def _legend(item: rumps.MenuItem, key: str, text: str) -> rumps.MenuItem:
        """A heading inside a submenu.

        Two tiers of heading, both letter spaced so they read as printed
        labels rather than as data. The sections at the top of the menu are
        upper case; these are sentence case, because several of them carry a
        project or profile name and upper casing somebody's directory is a lie
        about what it is called.
        """
        row = rumps.MenuItem(f"lg:{key}", callback=None)
        _apply_style(row, [("  ", "dim"), (text, "head")], mono=False)
        item.add(row)
        return row

    @staticmethod
    def _line(item: rumps.MenuItem, key: str, label: str, callback=None,
              tone: str = "text", indent: str = "  ") -> rumps.MenuItem:
        """One row of a submenu, drawn in the same face as everything else.

        Rows built as plain titles came out in the 13 point system font while
        the styled rows around them were 12 point monospaced, so the actions at
        the bottom of a menu looked like they belonged to another program.
        """
        row = rumps.MenuItem(key, callback=callback)
        _apply_style(row, [(indent, "dim"), (label, tone)], mono=False)
        item.add(row)
        return row

    def _more(self, hidden: int, tag: str) -> None:
        """Say what the list stopped short of, where it stopped.

        The heading above counts every session; this counts the ones that did
        not fit. Without it the two numbers disagreed in silence, and the list
        simply ended, which reads as "that is all of them".
        """
        row = rumps.MenuItem(f"more:{tag}", callback=None)
        _apply_style(row, [("  ", "dim"),
                           (f"{hidden} more, idle the longest", "dim")], mono=False)
        self.menu.add(row)

    def _running_block(self, item: rumps.MenuItem, here: list, empty: str,
                       tag: str) -> None:
        """The sessions running on whatever this menu is about.

        A subscription row says how much of a window is gone; a profile row
        says how many projects it covers. Neither says who is spending it,
        which is the question those numbers raise. Each line opens the tab it
        names, so the account at 95% is one click from the terminal burning it.
        """
        if here:
            self._legend(item, f"run:{tag}", f"Running now \u00b7 {len(here)}")
        else:
            self._line(item, f"runhead:{tag}", empty, tone="dim")
        # This list was the only one with no limit at all, and it is the one
        # most able to grow: every session on a subscription, or every session
        # under a profile. Measured at 39 rows it stood 907 points tall, which
        # overflows a laptop screen into the scroll arrows.
        # Twenty for the submenu's own rows. A subscription menu spends about
        # twelve on its heading, usage and actions, and a profile menu spends
        # more on its project list, so the count is the larger of the two: a
        # list that stops a little early is better than one that scrolls.
        here, hidden = _capped(here, _rows_that_fit() - 20)
        for sess in here:
            reachable = bool(focus.bundle_for_program(sess.term_program) and sess.tty)
            notes = bool(sess.context_tokens or sess.spent.total or sess.model)
            # A row with a submenu cannot also be clicked, so the jump to the
            # tab moves inside the submenu rather than being lost. A session
            # with nothing to show and nowhere to go stays a plain row.
            detailed = notes or reachable
            row = rumps.MenuItem(
                f"run:{tag}:{sess.pid}",
                callback=None)
            _apply_style(row, _session_line(sess))
            if detailed:
                self._session_notes(row, sess, reachable, tag)
            item.add(row)
        if hidden:
            row = rumps.MenuItem(f"more:{tag}", callback=None)
            _apply_style(row, [("    ", "dim"),
                               (f"{hidden} more, idle the longest", "dim")],
                         mono=False)
            item.add(row)

    def _usage_block(self, item: rumps.MenuItem, acct: core.Account) -> None:
        """Every window this account has, spelled out.

        The row above carries the same three, abbreviated to fit beside four
        other accounts. Here there is room for what got dropped: when each one
        comes back as a clock time as well as a countdown, and how far through
        its cycle it already is, which is the reading that says whether the
        spending is ahead of the clock or behind it.
        """
        if not acct.reading:
            return
        self._legend(item, f"use:{acct.name}", "Usage")
        for index, lim in enumerate(acct.limits):
            spent = lim.spent
            if spent is None:
                continue
            when = _compact_reset(lim.resets_at) if lim.resets_at else "unused"
            at = _clock_time(lim.resets_at)
            label = f"{lim.scope} {_span_label(lim)}" if lim.scope else lim.label
            # Multiple models can share a kind, so each window needs its own key.
            self._spec(item, f"lim:{acct.name}:{lim.kind}:{index}", label,
                       f"{spent:.0f}%", _tone(spent),
                       after=[("".join(t for t, *_ in _gauge(spent, BAR_W, "ok")), "mono"),
                              (f"  resets in {when}" if lim.resets_at else "  unused", "dim"),
                              (f" ({at})" if at else "", "dim")])
        if acct.is_codex and acct.extras.get("has_5h") is False:
            self._line(item, f"no5h:{acct.name}", "This plan has no 5h window.", tone="dim")
        item.add(rumps.separator)
        if acct.is_codex and acct.extras:
            balance = str(acct.extras.get("credits_balance") or "")
            balance_text = ("none" if not acct.extras.get("has_credits")
                            or balance in ("", "0") else balance)
            n = acct.extras.get("reset_credits", 0)
            note = f"  {n} reset credit{'s' if n != 1 else ''}" if n else ""
            if acct.extras.get("reset_credits_applicable") == 0 and n > 0:
                note += ", none apply right now"
            self._spec(item, f"credits:{acct.name}", "credits", balance_text, "dim",
                       after=[(note, "dim")] if note else [])
            item.add(rumps.separator)

    @staticmethod
    def _spec(row: rumps.MenuItem, key: str, label: str, figure: str,
              tone: str = "text", after=()) -> None:
        """One line of a specification: what it is, then what it reads."""
        note = rumps.MenuItem(key, callback=None)
        try:
            note._menuitem.setAttributedTitle_(_spec_line(label, figure, tone, after))
        except Exception:
            pass
        row.add(note)

    def _session_notes(self, row: rumps.MenuItem, sess: sessions.Session,
                       reachable: bool, tag: str) -> None:
        """What the condensed row leaves out, laid out as a specification.

        This was a stack of sentences, every one of them in the same grey,
        each starting in a different place and ending in a different place:
        "Context now: 573,398 of 1,000,000 (57% full)". Read as a paragraph
        that is fine. Scanned, which is what a panel is for, it gives the eye
        nothing to line up on.

        So it is a table, under two headings, with the labels in one column
        and the figures right aligned in another. Four numbers of wildly
        different size compare by where they end rather than by counting
        digits, which is the only reason cache reads dominating a total is
        visible at all.
        """
        pid, t = sess.pid, sess.spent

        if sess.context_tokens:
            self._legend(row, f"ctx:{tag}:{pid}", "Context")
            self._spec(row, f"note:{tag}:{pid}:ctx", "in use",
                       f"{sess.context_tokens:,}", "text",
                       after=[(f" / {sess.window:,}", "dim"),
                              *[(txt, "mono") for txt, _ in
                                _context_bar(sess, named=False)]])
            if sess.model:
                self._spec(row, f"note:{tag}:{pid}:model", "model",
                           "", "dim", after=[(sess.model, "text")])

        if t.total:
            self._legend(row, f"spend:{tag}:{pid}", "Tokens spent")
            # Kept apart because they are not interchangeable: a cache read
            # costs a fraction of a fresh input token, and a long conversation
            # re-reads its whole context every turn, so cache reads dominate
            # the total and one number hides what was really spent.
            for label, value in (("input", t.input), ("cache write", t.cache_write),
                                 ("cache read", t.cache_read), ("output", t.output)):
                self._spec(row, f"note:{tag}:{pid}:{label}", label, f"{value:,}")
            self._spec(row, f"note:{tag}:{pid}:total", "total", f"{t.total:,}",
                       _spent_tone(t.total),
                       after=[(f" \u00b7 {t.turns:,} turns", "dim")])

        if reachable:
            row.add(rumps.separator)
            go = self._line(row, f"go:{tag}:{pid}", "Take me to that tab",
                            callback=self._make_reveal(sess))
            _set_icon(go, "arrow.up.forward.app")

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
        head = rumps.MenuItem(f"sc:{scope}:{key}", callback=None)
        _apply_style(head, [("  ", "dim"), (title, "head")], mono=False)
        rows = [head]
        for acct in snap.accounts:
            if acct.is_codex or not acct.signed_in:
                continue
            same = acct.name == current
            # The plain title has to be unique inside one menu, and it is what
            # macOS matches when you type. Scope first, so typing picks a row
            # rather than the first account with that name under any heading.
            # The row already in use gets a callback that does nothing rather
            # than no callback at all. A menu item with no action is disabled,
            # and macOS draws a disabled row's whole attributed title at
            # reduced alpha, so the account you are actually on was the one
            # row in the list whose figures were hard to read, and its green
            # came out a pale green that looked like a third state.
            entry = rumps.MenuItem(f"{scope}:{key}:{acct.name}",
                                   callback=_nothing if same else
                                   self._make_assign(scope, key, acct.name, cwd))
            # All three windows, not only the five hour one. Picking a
            # subscription for a session is the decision this list exists to
            # serve, and one number out of three cannot settle it: an account
            # at 5% of its five hours can still be the wrong choice if its
            # week is nearly gone.
            _apply_style(entry, [*_chip(acct.name), *_windows(acct)],
                         mono=False, tabs=self._pick_tabs)
            # A real tick, in the gutter macOS ticks every other menu in. It
            # used to be a check character in front of the chip, which moved
            # the chip of the account already in use two columns right of
            # every other chip in the list.
            entry._menuitem.setState_(1 if same else 0)
            rows.append(entry)
        if clearable:
            drop = rumps.MenuItem(f"clear:{scope}:{key}",
                                  callback=self._make_clear(scope, key, cwd))
            _apply_style(drop, [("    ", "dim"), ("  ", "text"),
                                ("Remove this rule", "text")], mono=False)
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

    def _did(self, ok: bool, message: str, applied: dict | None = None) -> None:
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

    def _reflect_rules(self, applied: dict | None = None) -> None:
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
        if self._menu_open:
            self._alerts.append(message)
            return
        rumps.alert(title="Claude Code Accounts", message=message, ok="OK")

    # ------------------------------------------------------------------ actions

    def _make_poke(self, account: str):
        def handler(_sender):
            # Clicking closes the menu, and the request that follows can take
            # 45 seconds, so say the click landed before anything is waited on.
            # Without this the only sign of a running request was a row in a
            # menu that was no longer on screen.
            self._report(True, f"{account}: starting its windows…")
            # Up to a 45 second request. Never on the drawing thread.
            threading.Thread(target=self._poke, args=(account,), daemon=True).start()
        return handler

    def _poke(self, account: str) -> None:
        ok, msg = core.poke(account)
        self._later(lambda: self._poked(ok, account, msg))

    def _poked(self, ok: bool, account: str, message: str) -> None:
        self._report(ok, f"{account}: {message}")
        if ok:
            # Forced: the point of the click is the window it started, and an
            # ordinary refresh is skipped entirely while the account is inside
            # a rate limit, which would leave the row reading "idle" with no
            # way to tell that from a click that did nothing.
            self._on_refresh_tick(None, force=True)
            # The request that opens a window returns before the usage endpoint
            # reports it, so one check a few seconds later catches what the
            # first one missed.
            threading.Timer(12.0, lambda: self._later(lambda: self._poke_again(account))).start()
        else:
            # A failure has to interrupt. The menu closed on the click, and the
            # flash row above lives 30 seconds in a menu nobody is looking at,
            # so a poke that failed was indistinguishable from one that was
            # never wired up.
            self._notify(f"Could not start the 5h window for {account}.\n\n{message}")

    def _poke_again(self, account: str) -> None:
        acct = next((a for a in self._snapshot.accounts if a.name == account), None)
        if acct and acct.reading and any(not lim.resets_at for lim in acct.limits):
            core.forget_usage(account)
            self._on_refresh_tick(None, force=True)

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
            rumps.notification("Claude Code Accounts", "Renamed" if ok else "Rename failed", msg)
            self.refresh_now(None)
        return handler

    def _make_recolor(self, account: str, index: int):
        def handler(_sender):
            core.set_chip_index(account, index)
            self._rebuild()          # a colour is local; nothing to ask the API
        return handler

    def _make_remove(self, account: str):
        def handler(_sender):
            acct = next((a for a in self._snapshot.accounts if a.name == account), None)
            title = f"Remove {account}?"
            message = ("This deletes its stored login from the keychain. Your "
                       "subscription is untouched, and you can add it back with a sign-in.")
            if acct and acct.is_codex:
                if os.path.islink(acct.slot):
                    title = f"Stop tracking {account}?"
                    message = ("This removes the account from the app. "
                               "The login in ~/.codex is untouched.")
                else:
                    message = ("This deletes its login file. Your subscription is untouched, "
                               "and you can add it back with a sign-in.")
            confirm = rumps.alert(title=title, message=message, ok="Remove", cancel="Cancel")
            if confirm == 1:
                core.remove_account(account)
                self.refresh_now(None)
        return handler

    def _make_add(self, name: str = ""):
        def handler(_sender):
            self._add_account(None, preset=name)
        return handler

    def _make_add_provider(self, provider: str):
        def handler(_sender):
            self._add_account(None, provider=provider)
        return handler

    def _browser_menu(self, title: str, name: str, provider: str = "claude") -> rumps.MenuItem:
        """Pick the browser to sign in with.

        Which browser matters: it signs in as whoever that browser is already
        logged into, and with several subscriptions that is exactly how the
        wrong account gets attached to a name.
        """
        menu = rumps.MenuItem(title)
        for label, app in oauth.installed_browsers():
            menu.add(rumps.MenuItem(label, callback=self._make_sign_in(name, app, provider)))
        return menu

    def _make_sign_in(self, name: str, app: str, provider: str = "claude"):
        def handler(_sender):
            self._sign_in(name, app, provider)
        return handler

    def _sign_in(self, name: str, app: str = "", provider: str = "claude") -> None:
        """Sign an account in. The browser hands the code back by itself."""
        try:
            cb = (oauth.Callback(port=codex.CALLBACK_PORT, path=codex.CALLBACK_PATH)
                  if provider == "codex" else oauth.Callback())
        except OSError as e:
            if provider == "codex":
                self._notify(f"Could not sign in as “{name}”. Port 1455 is in use. "
                             "Close any other sign-in or `codex login` and try again.")
            else:
                self._notify(f"Could not sign in as “{name}”. The app could not open "
                             f"a local port for the browser to call back on.\n\n{e}")
            return
        attempt = (core.sign_in_begin_codex(name) if provider == "codex"
                   else core.sign_in_begin(name, cb.redirect_uri))
        # The server is made before the attempt, because the authorize URL
        # needs the port, so it learns which sign-in it is waiting for only
        # now. Anything on this machine can reach that port; without this it
        # would take a redirect meant for some other sign-in.
        cb.expect(attempt.state)
        err = oauth.open_in(attempt.url, app)
        if err:
            cb.close()
            self._notify(f"Could not open a browser: {err}")
            return
        # Keyed to the attempt, so a second click on "Sign in again" while the
        # first tab is still open takes the account over. The first wait still
        # runs out five minutes later, and without the key it announced a
        # timeout for an account that had signed in by then.
        self._signing_in[name] = attempt
        # Clicking closes the menu, and the browser can sit open for minutes,
        # so say the click landed and mark the account until the code is back.
        self._report(True, f"Signing in as “{name}”…")

        def wait() -> None:
            # Every way out lands in _signed_in, because that is what clears
            # the mark. A raise here used to die with the thread; now it would
            # leave the row reading "signing in" for good.
            try:
                if not cb.wait(300):
                    outcome = (False, f"Signing in as “{name}” timed out. Try again.")
                elif cb.error or not cb.code:
                    outcome = (False, f"Sign-in was refused: {cb.error or 'no code came back'}")
                else:
                    outcome = (core.sign_in_finish_codex(attempt, cb.code, cb.state)
                               if provider == "codex" else
                               core.sign_in_finish(attempt, f"{cb.code}#{cb.state}"))
            except Exception as e:
                outcome = (False, f"Signing in as “{name}” failed: {e}")
            finally:
                cb.close()
            self._later(lambda: self._signed_in(name, attempt, *outcome))

        threading.Thread(target=wait, daemon=True).start()

    def _signed_in(self, name: str, attempt: oauth.Attempt, ok: bool, message: str) -> None:
        """Finish a sign-in on the main thread: drop the mark, say what happened.

        This used to end in an alert. An alert costs a click, and it hides the
        menu that already shows the account signed in. A failure still needs
        one: the menu closed on the click, and the flash row lives 30 seconds
        in a menu nobody is looking at.
        """
        if self._signing_in.get(name) is not attempt:
            return            # a newer attempt owns this account's mark
        del self._signing_in[name]
        self._report(ok, message)
        if ok:
            # Forced, for the reason a poke is: an account that just signed in
            # has numbers worth having now, and an ordinary refresh is skipped
            # outright while the account sits inside a rate limit.
            self._on_refresh_tick(None, force=True)
        else:
            self._notify(message)

    def _signing_row(self, item: rumps.MenuItem, name: str) -> None:
        """The row in an account's menu that says a sign-in is in flight."""
        if name not in self._signing_in:
            return
        row = rumps.MenuItem(f"signing:{name}", callback=None)
        _apply_style(row, [("  ", "dim"), ("Signing in…", "warn"),
                           ("   finish in the browser", "dim")])
        item.add(row)

    def _add_account(self, _sender, preset: str = "", provider: str = "claude") -> None:
        """Name an account before opening its provider's browser sign-in.

        The label chooses where the returned login belongs, so it must be
        settled before the browser opens. Existing rows already have a name.
        """
        name = preset
        if not name:
            win = rumps.Window(
                title="Add a Codex account" if provider == "codex" else "Add a Claude account",
                message=("Name this account (letters, digits, dashes). It is a label "
                         "for you, not the email.\nA browser opens to sign in to ChatGPT. "
                         "Use the browser that is signed in to the account you want."
                         if provider == "codex" else
                         "Name this account (letters, digits, dashes). It is a label "
                         "for you, not the email.\nA Terminal opens running Claude Code "
                         "as that account, where you type /login."),
                ok="Open browser" if provider == "codex" else "Open Terminal",
                cancel="Cancel", dimensions=(240, 22))
            resp = win.run()
            if resp.clicked != 1:
                return
            name = resp.text
        name = "".join(ch for ch in name.strip() if ch.isalnum() or ch in "-_")
        if not name:
            return
        self._sign_in(name, provider=provider)   # per-browser entries live on the row

    @staticmethod
    def _make_open(path: str):
        def handler(_sender):
            subprocess.run(["open", path], check=False)
        return handler


def main() -> None:
    ManagerApp().run()


if __name__ == "__main__":
    main()
