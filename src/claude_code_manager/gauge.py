"""The menu bar image: an account name and one battery per usage window.

Drawn with a drawing-handler NSImage, so AppKit re-renders it in whatever
appearance the menu bar has at the moment it paints. Label colours resolve at
draw time, which is what keeps one image right in both light and dark menu
bars and on every screen density.

It fills with the share of the window that is SPENT, and the number inside is
that same percentage, which is what the menu rows, the account pickers and the
API all say. It used to fill with what was left, on the theory that two
different shapes could not be confused. The number is what a reader carries
between the two surfaces, not the shape, so an untouched account showed 100 in
the menu bar and 0% in the menu, and 100 read as a full tank rather than as an
empty one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Menu bar space is shared with everything else the user runs, so every
# dimension here is the smallest that still reads at a glance. Captions sit
# above their battery rather than beside it: the bar has spare height and no
# spare width, and a label over a gauge is how a dashboard cluster reads.
HEIGHT = 18.0                 # status bar content height; the bar itself is 22
BATTERY_W, BATTERY_H = 22.0, 10.0
BATTERY_Y = 0.0
CAPTION_SIZE, CAPTION_Y = 6.5, 10.0
RESET_MARK_SIZE = 5.2         # the ↻ marks the number, so it sets under it
RESET_GAP = 2.0               # between a window's name and its countdown
# Every piece of text here is plain black or plain white, whichever the bar is
# drawn against. Small text needs more contrast than body text, not less, and
# AppKit's secondary and tertiary label colours are calibrated for 11pt and up;
# at 6.5pt on a menu bar they read as smudges.
#
# Stale numbers still have to be readable: "these are old" is a different
# statement from "these are gone". Fading the fills says it; fading the text
# with them only makes the item look broken.
DIM_ALPHA = 0.78
NUB_W, NUB_H = 1.5, 3.5
CHIP_H, CHIP_PAD = 14.0, 3.5
GAP = 4.0                     # between cells


@dataclass
class Cell:
    caption: str                # 5h, 7d, fable
    used: Optional[float]       # percent used, None when unknown
    tone: str                   # ok | warn | hot | dim
    reset: str = ""             # 45m, 3h, 5d; empty when the window has not started
    reset_tone: str = "dim"     # how much the countdown matters, not how long it is


def _tone_color(tone: str):
    import AppKit
    return {
        "ok": AppKit.NSColor.systemGreenColor(),
        "warn": AppKit.NSColor.systemOrangeColor(),
        "hot": AppKit.NSColor.systemRedColor(),
    }.get(tone, AppKit.NSColor.secondaryLabelColor())


def _bar_ink():
    """Plain black or white, whichever the menu bar is drawing against.

    Call this inside the drawing handler and nowhere else. AppKit's semantic
    colours resolve against the appearance current when the colour is made,
    and this app has no window, so outside the handler they take the system
    setting. macOS picks the menu bar's appearance from the desktop behind it,
    so with Dark Mode on and a bright wallpaper the two disagree: the bar
    draws its own clock in black while labelColor still hands back white.
    """
    import AppKit
    return AppKit.NSColor.whiteColor() if _dark_now() else AppKit.NSColor.blackColor()


def _reset_color(tone: str, ink):
    """A countdown reads as its window's name until it starts to matter.

    _reset_tone upstream turns it a real colour only once the bucket is nearly
    spent, which is the point at which "when do I get it back" becomes the
    number being read. Until then it is the same ink as the name beside it:
    the ↻ already says which of the two is the countdown, so making it fainter
    buys no clarity and costs legibility.
    """
    return ink if tone == "dim" else _tone_color(tone)


def _reset_text(reset: str, color):
    """↻ plus a countdown, with the mark set smaller than the number it marks."""
    import AppKit
    out = AppKit.NSMutableAttributedString.alloc().initWithAttributedString_(
        _text(f"\u21bb{reset}", CAPTION_SIZE, AppKit.NSFontWeightRegular,
              color, mono_digits=True))
    out.addAttributes_range_(
        {AppKit.NSFontAttributeName:
            AppKit.NSFont.systemFontOfSize_weight_(RESET_MARK_SIZE,
                                                  AppKit.NSFontWeightRegular),
         AppKit.NSBaselineOffsetAttributeName: 0.4},
        AppKit.NSMakeRange(0, 1))
    return out


def _text(s: str, size: float, weight, color, mono_digits: bool = False):
    import AppKit
    if mono_digits:
        font = AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight)
    else:
        font = AppKit.NSFont.systemFontOfSize_weight_(size, weight)
    return AppKit.NSAttributedString.alloc().initWithString_attributes_(
        s, {AppKit.NSFontAttributeName: font, AppKit.NSForegroundColorAttributeName: color})


def _dark_now() -> bool:
    """Whether the appearance being drawn right now is a dark one.

    Read at draw time because the drawing handler runs whenever the menu bar
    repaints, including after a light/dark switch.
    """
    import AppKit
    try:
        app = AppKit.NSAppearance.currentDrawingAppearance()
        best = app.bestMatchFromAppearancesWithNames_(
            [AppKit.NSAppearanceNameAqua, AppKit.NSAppearanceNameDarkAqua])
        return best == AppKit.NSAppearanceNameDarkAqua
    except Exception:
        return False


def _ink(chip_color):
    """The chip's text colour: the hue pulled toward the bar's text colour.

    The menu bar draws with vibrancy, where labelColor is a compositing
    trick rather than a plain colour and blending with it yields mud. Plain
    white or black blends predictably and lands where the menu rows do.
    """
    import AppKit
    rgb = chip_color.colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace()) or chip_color
    toward = AppKit.NSColor.whiteColor() if _dark_now() else AppKit.NSColor.blackColor()
    return rgb.blendedColorWithFraction_ofColor_(0.42, toward) or rgb


def _rounded(x: float, y: float, w: float, h: float, r: float):
    import AppKit
    return AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        AppKit.NSMakeRect(x, y, w, h), r, r)


def _draw_battery(x: float, cell: Cell, dim: bool) -> None:
    import AppKit
    label = _bar_ink()          # same ink as the captions above it
    frame = label.colorWithAlphaComponent_(0.28 if dim else 0.42)
    y = BATTERY_Y
    frame.setStroke()
    outline = _rounded(x + 0.5, y + 0.5, BATTERY_W - 1, BATTERY_H - 1, 3.0)
    outline.setLineWidth_(1.0)
    outline.stroke()

    used = None if cell.used is None else max(0.0, min(100.0, cell.used))
    inner_w = BATTERY_W - 4
    if used:
        w = max(2.0, inner_w * used / 100.0)
        # The number sits on top, so the fill stays translucent: a solid swatch
        # would swallow the digits in whichever mode it contrasts less with.
        _tone_color(cell.tone).colorWithAlphaComponent_(0.28 if dim else 0.55).setFill()
        _rounded(x + 2, y + 2, w, BATTERY_H - 4, 1.5).fill()

    if used is None:
        s = "?"
        color = _bar_ink()
    else:
        s = f"{used:.0f}"
        color = _tone_color("hot") if used >= 100 else label
    if dim:
        color = color.colorWithAlphaComponent_(DIM_ALPHA)
    num = _text(s, 7.5, AppKit.NSFontWeightBold, color, mono_digits=True)
    size = num.size()
    # Centre the digits themselves, not their line box: the box carries
    # descender space no digit uses, which would push the number upward.
    font = AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(7.5, AppKit.NSFontWeightBold)
    baseline = y + (BATTERY_H - font.capHeight()) / 2
    num.drawAtPoint_(AppKit.NSMakePoint(x + (BATTERY_W - size.width) / 2,
                                        baseline - abs(font.descender())))


def status_image(name: str, chip_color, cells: list[Cell], dim: bool = False):
    """One image for the status item: the account chip, then a battery per window."""
    import AppKit
    # Same badge as the menu rows: a faint wash of the account's hue behind
    # text in that hue, so the bar and the list agree on who is paying. The
    # text colour depends on the appearance, so it is chosen inside draw().
    wash = chip_color.colorWithAlphaComponent_(0.12 if dim else 0.22)

    def head(c: Cell):
        """A window's name and its countdown, built where they are drawn.

        Both take their colour from the menu bar's appearance, which is only
        knowable inside the drawing handler; see _bar_ink. Metrics do not
        depend on colour, so the same call also sizes the cell up front.
        """
        ink = _bar_ink()
        if dim:
            ink = ink.colorWithAlphaComponent_(DIM_ALPHA)
        cap = _text(c.caption, CAPTION_SIZE, AppKit.NSFontWeightMedium, ink)
        if not c.reset:
            return cap, None
        color = _reset_color(c.reset_tone, ink)
        if dim and c.reset_tone != "dim":
            color = color.colorWithAlphaComponent_(DIM_ALPHA)
        return cap, _reset_text(c.reset, color)

    def head_width(cap, res) -> float:
        # A countdown can be wider than the battery under it (a five-letter
        # model name beside 45m), so a cell takes the wider of the two and
        # centres the narrower one in it. Nothing clips, columns stay square.
        return cap.size().width + (RESET_GAP + res.size().width if res else 0)

    battery_w = BATTERY_W + 1 + NUB_W

    def name_text():
        ink = _ink(chip_color)
        return _text(name, 10.0, AppKit.NSFontWeightMedium,
                     ink.colorWithAlphaComponent_(DIM_ALPHA) if dim else ink)

    chip_w = name_text().size().width + 2 * CHIP_PAD
    widths = [max(battery_w, head_width(*head(c))) for c in cells]
    width = 1 + chip_w + sum(GAP + w for w in widths) + 1

    def draw(_rect) -> bool:
        x = 1.0
        wash.setFill()
        _rounded(x, (HEIGHT - CHIP_H) / 2, chip_w, CHIP_H, 3.0).fill()
        name_str = name_text()
        sz = name_str.size()
        name_str.drawAtPoint_(AppKit.NSMakePoint(x + CHIP_PAD, (HEIGHT - sz.height) / 2))
        x += chip_w
        for cell, cell_w in zip(cells, widths):
            x += GAP
            cap, res = head(cell)
            hx = x + (cell_w - head_width(cap, res)) / 2
            cap.drawAtPoint_(AppKit.NSMakePoint(hx, CAPTION_Y))
            if res is not None:
                res.drawAtPoint_(AppKit.NSMakePoint(
                    hx + cap.size().width + RESET_GAP, CAPTION_Y))
            _draw_battery(x + (cell_w - battery_w) / 2, cell, dim)
            x += cell_w
        return True

    img = AppKit.NSImage.imageWithSize_flipped_drawingHandler_(
        AppKit.NSMakeSize(round(width), HEIGHT), False, draw)
    img.setTemplate_(False)
    return img
