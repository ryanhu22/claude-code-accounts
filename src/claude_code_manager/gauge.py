"""The menu bar image: an account name and one battery per usage window.

Drawn with a drawing-handler NSImage, so AppKit re-renders it in whatever
appearance the menu bar has at the moment it paints. Label colours resolve at
draw time, which is what keeps one image right in both light and dark menu
bars and on every screen density.

A battery reads as "how much is left", so it fills with the remaining share of
the window and the number inside is the remaining percent. The menu rows
underneath still count what is used; the two idioms are different shapes on
purpose, so they cannot be mistaken for each other.
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
NUB_W, NUB_H = 1.5, 3.5
CHIP_H, CHIP_PAD = 14.0, 3.5
GAP = 4.0                     # between cells


@dataclass
class Cell:
    caption: str                # 5h, 7d, fable
    used: Optional[float]       # percent used, None when unknown
    tone: str                   # ok | warn | hot | dim


def _tone_color(tone: str):
    import AppKit
    return {
        "ok": AppKit.NSColor.systemGreenColor(),
        "warn": AppKit.NSColor.systemOrangeColor(),
        "hot": AppKit.NSColor.systemRedColor(),
    }.get(tone, AppKit.NSColor.secondaryLabelColor())


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
    label = AppKit.NSColor.labelColor()
    frame = label.colorWithAlphaComponent_(0.28 if dim else 0.42)
    y = BATTERY_Y
    frame.setStroke()
    outline = _rounded(x + 0.5, y + 0.5, BATTERY_W - 1, BATTERY_H - 1, 3.0)
    outline.setLineWidth_(1.0)
    outline.stroke()
    frame.setFill()
    _rounded(x + BATTERY_W + 1, y + (BATTERY_H - NUB_H) / 2, NUB_W, NUB_H, 0.75).fill()

    left = None if cell.used is None else max(0.0, min(100.0, 100.0 - cell.used))
    inner_w = BATTERY_W - 4
    if left is not None and left > 0:
        w = max(2.0, inner_w * left / 100.0)
        # The number sits on top, so the fill stays translucent: a solid swatch
        # would swallow the digits in whichever mode it contrasts less with.
        _tone_color(cell.tone).colorWithAlphaComponent_(0.28 if dim else 0.55).setFill()
        _rounded(x + 2, y + 2, w, BATTERY_H - 4, 1.5).fill()

    if left is None:
        s = "?"
        color = AppKit.NSColor.secondaryLabelColor()
    else:
        s = f"{left:.0f}"
        color = _tone_color("hot") if left <= 0 else label
    if dim:
        color = color.colorWithAlphaComponent_(0.6)
    num = _text(s, 7.5, AppKit.NSFontWeightBold, color, mono_digits=True)
    size = num.size()
    num.drawAtPoint_(AppKit.NSMakePoint(x + (BATTERY_W - size.width) / 2,
                                        y + (BATTERY_H - size.height) / 2 + 0.5))


def status_image(name: str, chip_color, cells: list[Cell], dim: bool = False):
    """One image for the status item: the account chip, then a battery per window."""
    import AppKit
    secondary = AppKit.NSColor.secondaryLabelColor()
    # Same badge as the menu rows: a faint wash of the account's hue behind
    # text in that hue, so the bar and the list agree on who is paying. The
    # text colour depends on the appearance, so it is chosen inside draw().
    wash = chip_color.colorWithAlphaComponent_(0.12 if dim else 0.22)
    captions = [_text(c.caption, CAPTION_SIZE, AppKit.NSFontWeightMedium, secondary)
                for c in cells]

    def name_text():
        ink = _ink(chip_color)
        return _text(name, 10.0, AppKit.NSFontWeightMedium,
                     ink.colorWithAlphaComponent_(0.6) if dim else ink)

    chip_w = name_text().size().width + 2 * CHIP_PAD
    cell_w = BATTERY_W + 1 + NUB_W
    width = 1 + chip_w + len(cells) * (GAP + cell_w) + 1

    def draw(_rect) -> bool:
        x = 1.0
        wash.setFill()
        _rounded(x, (HEIGHT - CHIP_H) / 2, chip_w, CHIP_H, 3.0).fill()
        name_str = name_text()
        sz = name_str.size()
        name_str.drawAtPoint_(AppKit.NSMakePoint(x + CHIP_PAD, (HEIGHT - sz.height) / 2))
        x += chip_w
        for cap, cell in zip(captions, cells):
            x += GAP
            cs = cap.size()
            cap.drawAtPoint_(AppKit.NSMakePoint(x + (BATTERY_W - cs.width) / 2, CAPTION_Y))
            _draw_battery(x, cell, dim)
            x += cell_w
        return True

    img = AppKit.NSImage.imageWithSize_flipped_drawingHandler_(
        AppKit.NSMakeSize(round(width), HEIGHT), False, draw)
    img.setTemplate_(False)
    return img
