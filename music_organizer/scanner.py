"""Find audio files, read what they already know, and clean up messy names."""

from __future__ import annotations

import re
from pathlib import Path

import mutagen

from .models import Track

AUDIO_EXTS = {".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".opus"}

_TLD = r"(?:com|pk|info|in|net|org|co|me|cc|io|fm|to|xyz|live|mobi|link|biz|ws|tk|site|club|online|top)"
_DOMAIN = re.compile(rf"\b(?:www\.)?[\w-]+(?:\.[\w-]+)*\.{_TLD}\b", re.I)
# Words that are noise wherever they appear.
_JUNK_WORDS = re.compile(
    r"\b(?:\d{2,3}\s*kbps|kbps|vbr|songs?\s?pk|pagalworld|djmaza|mr-?jatt|webmusic"
    r"|wapking|downloadming|bestwap|mymp3song|djpunjab|freshmaza|isongs|starmusiq"
    r"|masstamilan)\b",
    re.I,
)
# Words that are noise inside brackets or at the very end of a name.
_SOFT_JUNK = re.compile(
    r"\b(?:320|128|192|256|official|music|video|lyrics?|lyrical|audio|full\s+song|song"
    r"|hd|hq|4k|1080p|720p|mp3|download(?:ed)?|remastered|high\s+quality)\b",
    re.I,
)
_TRAILING_SOFT = re.compile(
    r"(?:\s*[-|]?\s*\b(?:official(?:\s+(?:music|lyric(?:al)?|full))?\s*(?:video|audio)"
    r"|lyric(?:al)?s?\s*video|full\s+(?:video\s+)?song|full\s+audio|hd|hq|4k|mp3"
    r"|320|128)\b)+\s*$",
    re.I,
)
_BRACKETS = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
_TRACKNO = re.compile(r"^\s*(?:track\s*)?(?:\d{1,3}\s*[-._)]\s*|\d{2,3}\s+)(?=\D)", re.I)
_WS = re.compile(r"\s+")


def clean_text(s: str) -> str:
    """Strip site watermarks, bitrates, track numbers and other filename noise."""
    if not s:
        return ""
    s = s.replace("_", " ")

    def _bracket(m):
        inner = m.group(1)
        if (_DOMAIN.search(inner) or _JUNK_WORDS.search(inner) or _SOFT_JUNK.search(inner)
                or not inner.strip()):
            return " "
        return m.group(0)

    s = _BRACKETS.sub(_bracket, s)
    s = _DOMAIN.sub(" ", s)
    s = _JUNK_WORDS.sub(" ", s)
    s = re.sub(r"^\s*(?:320|128|192|256)\s*[-_.]\s*", "", s)
    s = _TRAILING_SOFT.sub("", s.strip())
    s = _TRACKNO.sub("", s)
    s = re.sub(r"\s*-\s*-+\s*", " - ", s)
    s = _WS.sub(" ", s).strip(" -_.,~|")
    return s


def filename_parts(path: Path) -> tuple[str, list[str]]:
    """Return (cleaned full stem, list of ' - ' separated pieces)."""
    stem = clean_text(path.stem)
    parts = [p.strip() for p in re.split(r"\s+-\s+|\s*\|\s*", stem) if p.strip()]
    parts = [p for p in parts if not p.isdigit()]
    return stem, parts


def scan(folder: Path, recursive: bool = True) -> list[Path]:
    it = folder.rglob("*") if recursive else folder.glob("*")
    files = [p for p in it if p.is_file() and p.suffix.lower() in AUDIO_EXTS
             and not p.name.startswith(".")]
    return sorted(files, key=lambda p: str(p).lower())


def _first(tags, key) -> str:
    try:
        v = tags.get(key)
    except Exception:
        return ""
    if not v:
        return ""
    return str(v[0] if isinstance(v, list) else v).strip()


def read_track(path: Path) -> Track:
    t = Track(path=path)
    try:
        f = mutagen.File(path, easy=True)
    except Exception as e:  # corrupt file
        t.message = f"unreadable: {e}"
        return t
    if f is None:
        t.message = "unsupported format"
        return t
    if f.info is not None:
        t.duration = getattr(f.info, "length", None)
    tags = f.tags or {}
    t.cur_title = _first(tags, "title")
    t.cur_artist = _first(tags, "artist")
    t.cur_album = _first(tags, "album")
    t.cur_year = _first(tags, "date")[:4]
    t.has_art = has_embedded_art(path)
    return t


def has_embedded_art(path: Path) -> bool:
    return extract_art(path) is not None


def extract_art(path: Path) -> bytes | None:
    try:
        f = mutagen.File(path)
    except Exception:
        return None
    if f is None:
        return None
    try:
        pics = getattr(f, "pictures", None)
        if pics:
            return pics[0].data
        tags = f.tags
        if tags is None:
            return None
        if hasattr(tags, "getall"):           # ID3
            apic = tags.getall("APIC")
            if apic:
                return apic[0].data
        if "covr" in tags:                     # MP4
            return bytes(tags["covr"][0])
        if "metadata_block_picture" in tags:  # Ogg
            import base64
            from mutagen.flac import Picture
            return Picture(base64.b64decode(tags["metadata_block_picture"][0])).data
    except Exception:
        return None
    return None
