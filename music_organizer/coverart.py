"""Cover art: normalise downloaded images, or paint a fresh one when none exists.

Generated covers use a random palette and one of several layouts, so every
missing-art song gets its own look, with the artist name and year on top.
"""

from __future__ import annotations

import colorsys
import functools
import io
import math
import random
import subprocess

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

SIZE = 1000


# --------------------------------------------------------------------------- #
# downloaded art
# --------------------------------------------------------------------------- #

def normalise(data: bytes | None, max_size: int = 1000) -> bytes | None:
    """Return JPEG bytes (RGB, <= max_size px) or None if it isn't an image."""
    if not data:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return None
    if im.width < 100 or im.height < 100:
        return None
    im = ImageOps.exif_transpose(im).convert("RGB")
    if max(im.size) > max_size:
        im.thumbnail((max_size, max_size), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90, optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# fonts
# --------------------------------------------------------------------------- #

_BOLD = ("Montserrat:black", "Montserrat:bold", "Poppins:bold", "Inter:bold",
         "Ubuntu:bold", "Noto Sans:bold", "Liberation Sans:bold", "DejaVu Sans:bold")
_REG = ("Montserrat:medium", "Poppins:medium", "Inter", "Ubuntu", "Noto Sans",
        "Liberation Sans", "DejaVu Sans")


@functools.lru_cache(maxsize=None)
def _font_file(choices: tuple[str, ...]) -> str | None:
    for pattern in choices:
        family = pattern.split(":")[0].lower()
        try:
            out = subprocess.run(["fc-match", "-f", "%{family}|%{file}", pattern],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        fam, _, path = out.partition("|")
        if family in fam.lower() and path:
            return path
    return None


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    path = _font_file(_BOLD if bold else _REG)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


# --------------------------------------------------------------------------- #
# palette + backgrounds
# --------------------------------------------------------------------------- #

def _hsv(h, s, v) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0, min(1, s)), max(0, min(1, v)))
    return int(r * 255), int(g * 255), int(b * 255)


def _palette(rng: random.Random) -> dict:
    h = rng.random()
    scheme = rng.choice(("analogous", "complement", "triad", "split"))
    off = {"analogous": 0.09, "complement": 0.5, "triad": 1 / 3, "split": 0.42}[scheme]
    return {
        "bright": _hsv(h, rng.uniform(0.55, 0.85), rng.uniform(0.85, 1.0)),
        "mid": _hsv(h + off, rng.uniform(0.55, 0.9), rng.uniform(0.6, 0.85)),
        "dark": _hsv(h + off / 2, rng.uniform(0.6, 0.95), rng.uniform(0.12, 0.28)),
        "deep": _hsv(h - 0.05, rng.uniform(0.5, 0.9), rng.uniform(0.05, 0.15)),
        "accent": _hsv(h + off + 0.5, rng.uniform(0.4, 0.7), 1.0),
    }


def _gradient(p: dict, rng: random.Random) -> Image.Image:
    corners = [p["bright"], p["mid"], p["dark"], p["deep"]]
    rng.shuffle(corners)
    small = Image.new("RGB", (2, 2))
    small.putdata(corners)
    return small.resize((SIZE, SIZE), Image.BICUBIC)


def _layer() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    im = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    return im, ImageDraw.Draw(im)


def _style_orbs(bg, p, rng):
    layer, d = _layer()
    for _ in range(rng.randint(3, 6)):
        r = rng.randint(180, 420)
        x, y = rng.randint(-100, SIZE + 100), rng.randint(-100, SIZE - 200)
        col = rng.choice([p["bright"], p["mid"], p["accent"]])
        d.ellipse((x - r, y - r, x + r, y + r), fill=(*col, rng.randint(110, 190)))
    layer = layer.filter(ImageFilter.GaussianBlur(70))
    bg.alpha_composite(layer)


def _style_vinyl(bg, p, rng):
    layer, d = _layer()
    cx, cy = rng.randint(560, 760), rng.randint(220, 420)
    R = rng.randint(380, 470)
    d.ellipse((cx - R, cy - R, cx + R, cy + R), fill=(12, 12, 16, 235))
    for r in range(R - 12, 120, -7):
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255, 14), width=1)
    lab = int(R * 0.34)
    d.ellipse((cx - lab, cy - lab, cx + lab, cy + lab), fill=(*p["bright"], 255))
    d.ellipse((cx - 10, cy - 10, cx + 10, cy + 10), fill=(10, 10, 10, 255))
    # sheen
    sheen, sd = _layer()
    sd.pieslice((cx - R, cy - R, cx + R, cy + R), 200, 235, fill=(255, 255, 255, 40))
    sheen = sheen.filter(ImageFilter.GaussianBlur(18))
    shadow = layer.split()[3].point(lambda a: int(a * 0.6))
    sh = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 255))
    sh.putalpha(shadow.filter(ImageFilter.GaussianBlur(30)))
    bg.alpha_composite(sh, (18, 24))
    bg.alpha_composite(layer)
    bg.alpha_composite(sheen)


def _style_sun(bg, p, rng):
    layer, d = _layer()
    cx, cy, R = SIZE // 2, rng.randint(330, 420), rng.randint(250, 320)
    top, bot = p["accent"], p["bright"]
    for y in range(cy - R, cy + R):
        t = (y - (cy - R)) / (2 * R)
        col = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        half = int(math.sqrt(max(0, R * R - (y - cy) ** 2)))
        # synthwave slits in the lower half
        if y > cy and ((y - cy) // 14) % 2 == 1 and (y - cy) < R * 0.9:
            continue
        d.line((cx - half, y, cx + half, y), fill=(*col, 255))
    glow = layer.filter(ImageFilter.GaussianBlur(40))
    bg.alpha_composite(glow)
    bg.alpha_composite(layer)
    # perspective grid floor
    g, gd = _layer()
    horizon = cy + int(R * 0.75)
    for i in range(1, 14):
        y = horizon + int((i / 13) ** 2 * (SIZE - horizon))
        gd.line((0, y, SIZE, y), fill=(*p["accent"], 90), width=2)
    for i in range(-12, 13):
        gd.line((cx + i * 30, horizon, cx + i * 160, SIZE), fill=(*p["accent"], 70), width=2)
    bg.alpha_composite(g)


def _style_waves(bg, p, rng):
    for k in range(rng.randint(5, 8)):
        layer, d = _layer()
        base = 150 + k * rng.randint(70, 110)
        amp, freq, phase = rng.randint(25, 80), rng.uniform(1.0, 2.5), rng.uniform(0, 6.3)
        pts = [(x, base + amp * math.sin(freq * x / SIZE * 2 * math.pi + phase))
               for x in range(0, SIZE + 20, 20)]
        pts += [(SIZE, SIZE), (0, SIZE)]
        col = [p["bright"], p["mid"], p["accent"], p["dark"]][k % 4]
        d.polygon(pts, fill=(*col, 70))
        bg.alpha_composite(layer.filter(ImageFilter.GaussianBlur(2)))


def _style_stripes(bg, p, rng):
    layer, d = _layer()
    w = rng.randint(40, 90)
    cols = [p["bright"], p["mid"], p["accent"]]
    for i, x in enumerate(range(-SIZE, SIZE * 2, w)):
        d.polygon([(x, 0), (x + w // 2, 0), (x + w // 2 + SIZE, SIZE), (x + SIZE, SIZE)],
                  fill=(*cols[i % 3], 55))
    bg.alpha_composite(layer)
    ring, rd = _layer()
    cx, cy, r = rng.randint(300, 700), rng.randint(250, 450), rng.randint(180, 300)
    rd.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255, 200), width=10)
    bg.alpha_composite(ring)


_STYLES = (_style_orbs, _style_vinyl, _style_sun, _style_waves, _style_stripes)


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #

def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _fit(draw, text, max_w, max_lines, start, stop, bold=True):
    for size in range(start, stop - 1, -4):
        f = _font(size, bold)
        lines = _wrap(draw, text, f, max_w)
        if len(lines) <= max_lines and all(draw.textlength(l, font=f) <= max_w for l in lines):
            return f, lines
    f = _font(stop, bold)
    lines = _wrap(draw, text, f, max_w)[:max_lines]
    return f, lines


def generate(artist: str, year: str = "", title: str = "", seed: int | None = None) -> bytes:
    rng = random.Random(seed)
    p = _palette(rng)
    bg = _gradient(p, rng).convert("RGBA")
    rng.choice(_STYLES)(bg, p, rng)

    # film grain
    noise = Image.effect_noise((SIZE, SIZE), 48).convert("L")
    grain = Image.new("RGBA", (SIZE, SIZE), (255, 255, 255, 0))
    grain.putalpha(noise.point(lambda v: int(v * 0.09)))
    bg.alpha_composite(grain)

    # legibility scrim at the bottom
    scrim = Image.new("L", (1, SIZE))
    for y in range(SIZE):
        t = max(0.0, (y - SIZE * 0.45) / (SIZE * 0.55))
        scrim.putpixel((0, y), int(215 * t ** 1.6))
    shade = Image.new("RGBA", (SIZE, SIZE), (*p["deep"], 255))
    shade.putalpha(scrim.resize((SIZE, SIZE)))
    bg.alpha_composite(shade)

    text_layer, d = _layer()
    margin = 72
    artist = (artist or "Unknown Artist").upper()
    af, alines = _fit(d, artist, SIZE - 2 * margin, 3, 150, 54)
    line_h = int(af.size * 1.08)

    title_txt = title.strip()
    tf, tlines = _fit(d, title_txt, SIZE - 2 * margin, 2, 46, 30, bold=False) if title_txt else (None, [])
    t_h = int(tf.size * 1.25) * len(tlines) if tf else 0

    y = SIZE - margin - t_h - line_h * len(alines)
    # accent bar + year chip
    if year:
        yf = _font(44)
        chip_w = int(d.textlength(year, font=yf)) + 44
        cy0 = y - 92
        d.rounded_rectangle((margin, cy0, margin + chip_w, cy0 + 64), radius=32,
                            fill=(*p["accent"], 235))
        d.text((margin + 22, cy0 + 32), year, font=yf, fill=p["deep"], anchor="lm")
    else:
        d.rectangle((margin, y - 44, margin + 120, y - 32), fill=(*p["accent"], 255))

    for line in alines:
        d.text((margin + 4, y + 5), line, font=af, fill=(0, 0, 0, 110))  # soft shadow
        d.text((margin, y), line, font=af, fill=(255, 255, 255, 255))
        y += line_h
    y += 10
    for line in tlines:
        d.text((margin, y), line, font=tf, fill=(255, 255, 255, 200))
        y += int(tf.size * 1.25)

    # big ghosted year in the corner
    if year:
        gf = _font(230)
        ghost, gd = _layer()
        gd.text((SIZE - 40, 40), year, font=gf, fill=(255, 255, 255, 46), anchor="ra")
        bg.alpha_composite(ghost)
    bg.alpha_composite(text_layer)

    out = io.BytesIO()
    bg.convert("RGB").save(out, "JPEG", quality=92, optimize=True)
    return out.getvalue()
