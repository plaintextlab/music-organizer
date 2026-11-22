"""High-level steps on a draft: cover art, new filename, write, undo."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import coverart, providers, tagger
from .matcher import _nobr
from .models import Status, Track
from .scanner import extract_art

JOURNAL = ".music-organizer-history.json"
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def split_artists(text: str) -> list[str]:
    return [a.strip() for a in re.split(r"\s*[,;]\s*", text or "") if a.strip()]


def new_filename(meta: dict, ext: str) -> str:
    """'Song Name (Year) - Artist.ext'"""
    names = meta.get("artists") or split_artists(meta.get("artist", ""))
    artist = ", ".join(names[:3]) + (" & more" if len(names) > 3 else "")
    name = meta.get("title", "").strip()
    if meta.get("year"):
        name += f" ({meta['year']})"
    if artist:
        name += f" - {artist}"
    name = _BAD_CHARS.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:200] + ext


def target_path(track: Track) -> Path | None:
    if not track.meta or not track.meta.get("title"):
        return None
    return track.path.with_name(new_filename(track.meta, track.ext))


# --------------------------------------------------------------------------- #
# cover art (all of these only change the draft)
# --------------------------------------------------------------------------- #

def prepare_cover(track: Track) -> None:
    """Online art > art already embedded > a freshly generated cover."""
    track.cover, track.cover_source = None, ""
    if download_cover(track):
        return
    existing = coverart.normalise(extract_art(track.path))
    if existing:
        track.cover, track.cover_source = existing, "existing"
        return
    if track.meta:
        generate_cover(track)


def download_cover(track: Track) -> bool:
    c = track.chosen
    if not c:
        return False
    urls = [c.art_url] if c.art_url else []
    # the same song from another source may have art when this one doesn't
    urls += [x.art_url for x in track.candidates
             if x is not c and x.art_url and _nobr(x.title) == _nobr(c.title)
             and x.score >= c.score - 10][:2]
    for url in urls:
        data = coverart.normalise(providers.download(url))
        if data:
            track.cover, track.cover_source = data, "online"
            return True
    return False


def generate_cover(track: Track) -> None:
    m = track.meta or {}
    names = m.get("artists") or split_artists(m.get("artist", ""))
    track.cover = coverart.generate(", ".join(names[:2]), m.get("year", ""), m.get("title", ""))
    track.cover_source = "generated"


def cover_from_file(track: Track, image: Path) -> None:
    data = coverart.normalise(image.expanduser().read_bytes())
    if not data:
        raise ValueError(f"{image.name} is not an image (or is smaller than 100px)")
    track.cover, track.cover_source = data, "file"


def keep_existing_cover(track: Track) -> bool:
    data = coverart.normalise(extract_art(track.path))
    if data:
        track.cover, track.cover_source = data, "existing"
    return bool(data)


# --------------------------------------------------------------------------- #
# writing - the only place music files are modified
# --------------------------------------------------------------------------- #

def _unique(path: Path, original: Path) -> Path:
    if path == original or not path.exists():
        return path
    if path.samefile(original):  # case-only rename
        return path
    stem, ext = path.stem, path.suffix
    for i in range(2, 100):
        p = path.with_name(f"{stem} [{i}]{ext}")
        if not p.exists():
            return p
    raise FileExistsError(path)


def write(track: Track, rename: bool = True) -> Path:
    """Write the draft's tags + art into the file, then rename it."""
    if not track.meta or not track.meta.get("title"):
        raise ValueError("draft has no title")
    meta = dict(track.meta)
    meta["artists"] = split_artists(meta.get("artist", ""))
    meta["artist"] = ", ".join(meta["artists"])
    for k in ("album", "albumartist", "year", "genre", "composer", "label"):
        meta.setdefault(k, "")
    tagger.write(track.path, meta, track.cover)
    old = new = track.path
    if rename:
        new = _unique(old.with_name(new_filename(meta, track.ext)), old)
        if new != old:
            old.rename(new)
            _journal(old, new)
    track.path = new
    track.status = Status.DONE
    track.approved = False
    track.message = f"written ({track.cover_source or 'no'} art)"
    return new


def _journal(old: Path, new: Path) -> None:
    j = old.parent / JOURNAL
    try:
        hist = json.loads(j.read_text()) if j.exists() else []
    except (OSError, ValueError):
        hist = []
    hist.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "from": old.name, "to": new.name})
    try:
        j.write_text(json.dumps(hist, indent=1, ensure_ascii=False))
    except OSError:
        pass


def undo_renames(folder: Path) -> list[tuple[Path, Path]]:
    """Reverse every rename recorded in history files under ``folder``."""
    done = []
    for j in sorted(folder.rglob(JOURNAL)):
        try:
            hist = json.loads(j.read_text())
        except (OSError, ValueError):
            continue
        remaining = []
        for e in reversed(hist):
            src, dst = j.parent / e["to"], j.parent / e["from"]
            if src.exists() and not dst.exists():
                src.rename(dst)
                done.append((src, dst))
            else:
                remaining.append(e)
        if remaining:
            j.write_text(json.dumps(list(reversed(remaining)), indent=1, ensure_ascii=False))
        else:
            j.unlink()
    return done
