"""Force every piece of metadata into clean, readable English (ASCII) text.

Hindi (Devanagari) and Bengali script are transliterated phonetically with
final-schwa deletion, so "तन्हा दिल" becomes "Tanha Dil" rather than
"tnhaa dil". Everything else goes through unidecode. HTML entities that some
APIs leak (``&quot;``) are decoded first.
"""

from __future__ import annotations

import html
import re
import unicodedata

from indic_transliteration import sanscript
from indic_transliteration.sanscript import transliterate
from unidecode import unidecode

_SCRIPTS = (
    # (regex for the block, sanscript scheme, consonant ranges)
    (re.compile(r"[ऀ-ॿ]"), sanscript.DEVANAGARI,
     ((0x0915, 0x0939), (0x0958, 0x095F))),
    (re.compile(r"[ঀ-৿]"), sanscript.BENGALI,
     ((0x0995, 0x09B9), (0x09DC, 0x09DF))),
)

_WS = re.compile(r"\s+")


def _ends_in_bare_consonant(word: str, ranges) -> bool:
    if not word:
        return False
    cp = ord(word[-1])
    return any(lo <= cp <= hi for lo, hi in ranges)


def _phonetic(iast: str, bengali: bool) -> str:
    """Turn scholarly IAST into the spelling people actually use."""
    s = iast.replace("ch", "\0").replace("c", "ch").replace("\0", "chh")
    s = s.replace("ś", "sh").replace("ṣ", "sh").replace("ṅ", "ng").replace("ñ", "n")
    s = s.replace("ṛ", "r").replace("r̥", "ri").replace("ḥ", "h")
    if bengali:
        s = s.replace("ṃ", "ng").replace("v", "b")
    else:
        s = re.sub(r"ṃ(?=[hkg])", "ng", s).replace("ṃ", "n").replace("m̐", "n")
    return s


def _indic_word(word: str, scheme, ranges) -> str:
    m = re.match(r"^(.*?)([^\w\u0900-\u09FF]*)$", word)
    word, tail = m.group(1), m.group(2)
    return _indic_core(word, scheme, ranges) + tail


def _indic_core(word: str, scheme, ranges) -> str:
    out = transliterate(word, scheme, sanscript.IAST)
    # Hindi/Bengali don't pronounce the inherent vowel at the end of a word.
    if _ends_in_bare_consonant(word, ranges) and out.endswith("a") and len(out) > 2:
        out = out[:-1]
    return _phonetic(out, bengali=scheme == sanscript.BENGALI)


def _needs_titlecase(original: str) -> bool:
    return any(rx.search(original) for rx, _, _ in _SCRIPTS)


def to_english(text: str | None) -> str:
    """Return an ASCII-only, human readable version of ``text``."""
    if not text:
        return ""
    text = html.unescape(str(text))
    text = unicodedata.normalize("NFC", text)
    indic = _needs_titlecase(text)

    if indic:
        words = []
        for word in text.split():
            for rx, scheme, ranges in _SCRIPTS:
                if rx.search(word):
                    word = _indic_word(word, scheme, ranges)
                    break
            words.append(word)
        text = " ".join(words)

    text = unidecode(text)
    # unidecode is ASCII already; drop control chars and stray symbols
    text = "".join(ch for ch in text if ch.isprintable())
    text = text.replace("[?]", "")
    text = _WS.sub(" ", text).strip(" -_,.")

    if indic:
        text = " ".join(w[:1].upper() + w[1:] for w in text.split())
    return text


def is_latin(text: str) -> bool:
    """True when the text is already readable Latin script."""
    text = html.unescape(text or "")
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    latin = sum(1 for c in letters if ord(c) < 0x250)
    return latin / len(letters) > 0.9


_FROM_RX = re.compile(r"\s*[\(\[]\s*from\s+[\"'“‘]?[^)\]]*[\)\]]", re.I)
_FEAT_RX = re.compile(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+([^)\]]*)[\)\]]", re.I)
_ALBUM_NOISE = re.compile(
    r"\s*[\(\[](?:original\s+(?:motion\s+picture\s+|film\s+|television\s+)?"
    r"soundtrack|o\.?s\.?t\.?|deluxe(?:\s+(?:edition|version))?|expanded\s+edition"
    r"|from\s+[^)\]]*)[\)\]]",
    re.I,
)


def split_featuring(title: str) -> tuple[str, list[str]]:
    """'Song (feat. X & Y)' -> ('Song', ['X', 'Y'])"""
    feats: list[str] = []
    def grab(m):
        feats.extend(p.strip() for p in re.split(r"\s*(?:,|&| and )\s*", m.group(1)) if p.strip())
        return ""
    return _FEAT_RX.sub(grab, html.unescape(title or "")), feats


def clean_album(album: str) -> str:
    a = _ALBUM_NOISE.sub("", html.unescape(album or ""))
    a = re.sub(r"\s*-\s*(Single|EP)$", "", a)
    return to_english(a)


def clean_title(title: str) -> str:
    """Romanize and strip compilation noise like ``(From "Movie")``."""
    t = html.unescape(title or "")
    t = _FROM_RX.sub("", t)
    return to_english(t)
