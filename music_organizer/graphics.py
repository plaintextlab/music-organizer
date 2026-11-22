"""Detect whether the terminal can show real images (kitty graphics / sixel).

This must be imported *before* the Textual app starts: textual-image asks the
terminal what it supports, which is impossible once Textual owns the screen.

    MODE          "kitty graphics" | "sixel" | ""   ("" = not supported)
    ImageWidget   Textual widget that draws a full-resolution image, or None

MUSIC_ORGANIZER_PREVIEW=blocks or =ueberzug skips this check.
"""

from __future__ import annotations

import os

MODE = ""
ImageWidget = None

if os.environ.get("MUSIC_ORGANIZER_PREVIEW", "").lower() not in (
        "blocks", "off", "none", "ueberzug", "ueberzugpp"):
    try:
        from textual_image.renderable import Image as _Best
        from textual_image.renderable import SixelImage as _Sixel
        from textual_image.renderable import TGPImage as _TGP

        if _Best is _TGP or _Best is _Sixel:
            from textual_image.widget import Image as ImageWidget  # noqa: F811
            MODE = "kitty graphics" if _Best is _TGP else "sixel"
    except Exception:  # never let image support stop the app from starting
        MODE, ImageWidget = "", None
