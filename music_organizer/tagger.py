"""Write clean tags + cover art. Old tags (site watermarks etc.) are wiped first."""

from __future__ import annotations

import base64
from pathlib import Path

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (APIC, ID3, TALB, TCOM, TCON, TDRC, TIT2, TPE1, TPE2, TPUB,
                         ID3NoHeaderError)
from mutagen.mp4 import MP4, MP4Cover


def write(path: Path, meta: dict, cover: bytes | None) -> None:
    ext = path.suffix.lower()
    if ext == ".mp3":
        _write_mp3(path, meta, cover)
    elif ext in (".m4a", ".mp4"):
        _write_mp4(path, meta, cover)
    elif ext == ".flac":
        _write_flac(path, meta, cover)
    elif ext in (".ogg", ".opus"):
        _write_ogg(path, meta, cover)
    else:
        raise ValueError(f"unsupported format: {ext}")


def _write_mp3(path: Path, m: dict, cover: bytes | None) -> None:
    try:
        ID3(path).delete(path, delete_v1=True, delete_v2=True)
    except ID3NoHeaderError:
        pass
    tags = ID3()
    tags.add(TIT2(encoding=3, text=m["title"]))
    if m["artist"]:
        tags.add(TPE1(encoding=3, text=m["artist"]))
    if m["album"]:
        tags.add(TALB(encoding=3, text=m["album"]))
    if m["albumartist"]:
        tags.add(TPE2(encoding=3, text=m["albumartist"]))
    if m["year"]:
        tags.add(TDRC(encoding=3, text=m["year"]))
    if m["genre"]:
        tags.add(TCON(encoding=3, text=m["genre"]))
    if m["composer"]:
        tags.add(TCOM(encoding=3, text=m["composer"]))
    if m["label"]:
        tags.add(TPUB(encoding=3, text=m["label"]))
    if cover:
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
    # v2.3 is what car stereos, Windows and most phones read reliably
    tags.save(path, v2_version=3, v1=0)


def _write_mp4(path: Path, m: dict, cover: bytes | None) -> None:
    f = MP4(path)
    if f.tags is None:
        f.add_tags()
    f.tags.clear()
    pairs = {"\xa9nam": m["title"], "\xa9ART": m["artist"], "\xa9alb": m["album"],
             "aART": m["albumartist"], "\xa9day": m["year"], "\xa9gen": m["genre"],
             "\xa9wrt": m["composer"], "cprt": m["label"]}
    for k, v in pairs.items():
        if v:
            f.tags[k] = [v]
    if cover:
        f.tags["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    f.save()


def _vorbis_fields(m: dict) -> dict:
    return {k: v for k, v in {
        "title": m["title"], "artist": m["artist"], "album": m["album"],
        "albumartist": m["albumartist"], "date": m["year"], "genre": m["genre"],
        "composer": m["composer"], "organization": m["label"],
    }.items() if v}


def _picture(cover: bytes) -> Picture:
    from PIL import Image
    import io
    pic = Picture()
    pic.type = 3
    pic.mime = "image/jpeg"
    pic.desc = "Cover"
    pic.data = cover
    with Image.open(io.BytesIO(cover)) as im:
        pic.width, pic.height = im.size
    pic.depth = 24
    return pic


def _write_flac(path: Path, m: dict, cover: bytes | None) -> None:
    f = FLAC(path)
    f.delete()
    f.clear_pictures()
    for k, v in _vorbis_fields(m).items():
        f[k] = v
    if cover:
        f.add_picture(_picture(cover))
    f.save()


def _write_ogg(path: Path, m: dict, cover: bytes | None) -> None:
    f = mutagen.File(path)
    if f is None:
        raise ValueError("unreadable ogg file")
    if f.tags is None:
        f.add_tags()
    f.tags.clear()
    for k, v in _vorbis_fields(m).items():
        f.tags[k] = v
    if cover:
        f.tags["metadata_block_picture"] = [
            base64.b64encode(_picture(cover).write()).decode("ascii")]
    f.save()
