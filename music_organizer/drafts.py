"""Drafts live outside the music folder, so reviewing never touches your files.

    ~/.cache/music-organizer/drafts/<folder-id>/
        drafts.json            proposed tags + new filename for every song
        covers/<new name>.jpg  the proposed cover, viewable in any image viewer

A draft is reloaded on the next run as long as the song file is unchanged
(same size and modification time).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import threading
from pathlib import Path

from .models import Candidate, Status, Track
from .organizer import new_filename

BASE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "music-organizer"
ROOT = BASE / "drafts"
STATE = BASE / "state.json"


def remember_folder(folder: Path) -> None:
    """Remember the last opened folder so the next start reopens it."""
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"last_folder": str(folder)}))
    except OSError:
        pass


def last_folder() -> Path | None:
    try:
        p = Path(json.loads(STATE.read_text())["last_folder"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return p if p.is_dir() else None


def _cand_to_dict(c: Candidate) -> dict:
    return dataclasses.asdict(c)


def _cand_from_dict(d: dict) -> Candidate | None:
    fields = {f.name for f in dataclasses.fields(Candidate)}
    try:
        return Candidate(**{k: v for k, v in d.items() if k in fields})
    except TypeError:
        return None


def _id(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:12]


class DraftStore:
    def __init__(self, folder: Path):
        self.folder = folder.resolve()
        self.dir = ROOT / f"{re.sub(r'[^\w-]', '_', self.folder.name)[:40]}-{_id(str(self.folder))}"
        self.covers = self.dir / "covers"
        self.file = self.dir / "drafts.json"
        self._lock = threading.Lock()
        self._written: dict[str, int] = {}   # cover file -> id() of the bytes written

    # ------------------------------------------------------------------ io
    def _read(self) -> dict:
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return {}

    def _key(self, track: Track) -> str:
        try:
            return str(track.path.resolve().relative_to(self.folder))
        except ValueError:
            return str(track.path.resolve())

    @staticmethod
    def _stamp(track: Track) -> tuple[int, int]:
        st = track.path.stat()
        return st.st_size, int(st.st_mtime)

    def load(self, tracks: list[Track]) -> int:
        """Restore saved drafts onto freshly scanned tracks. Returns count."""
        data = self._read().get("drafts", {})
        n = 0
        for t in tracks:
            e = data.get(self._key(t))
            if not e:
                continue
            try:
                if tuple(e.get("stamp", ())) != self._stamp(t):
                    continue  # the file changed since the draft was made
            except OSError:
                continue
            t.meta = e.get("meta")
            t.status = Status(e.get("status", "review"))
            t.message = e.get("message", "")
            t.approved = bool(e.get("approved"))
            t.edited = bool(e.get("edited"))
            t.custom_query = e.get("query", "")
            t.queries = list(e.get("queries") or [])
            t.cover_source = e.get("cover_source", "")
            t.candidates = [c for c in map(_cand_from_dict, e.get("candidates") or []) if c]
            ch = _cand_from_dict(e["chosen"]) if e.get("chosen") else None
            # point "chosen" at the same object as in the list, so the match
            # picker and "web art" work exactly as before the restart
            t.chosen = next((c for c in t.candidates if c == ch), ch)
            cf = e.get("cover_file")
            if cf and (self.covers / cf).exists():
                t.cover = (self.covers / cf).read_bytes()
            n += 1
        return n

    def save(self, tracks: list[Track]) -> None:
        with self._lock:
            self._save(tracks)

    def _save(self, tracks: list[Track]) -> None:
        self.covers.mkdir(parents=True, exist_ok=True)
        old = self._read().get("drafts", {})
        out, keep_covers = {}, set()
        for t in tracks:
            if t.status in (Status.PENDING, Status.SEARCHING, Status.DONE) and not t.meta:
                continue
            if t.status == Status.DONE:
                continue
            try:
                stamp = self._stamp(t)
            except OSError:
                continue
            key = self._key(t)
            cover_file = ""
            if t.cover:
                base = new_filename(t.meta, "") if t.meta and t.meta.get("title") else t.path.stem
                cover_file = f"{base} [{_id(key)[:6]}].jpg"
                p = self.covers / cover_file
                if self._written.get(cover_file) != id(t.cover) or not p.exists():
                    if not p.exists() or p.read_bytes() != t.cover:
                        p.write_bytes(t.cover)
                    self._written[cover_file] = id(t.cover)
                keep_covers.add(cover_file)
            out[key] = {
                "stamp": list(stamp),
                # a song still being looked up comes back as "check", not stuck
                "status": (Status.REVIEW if t.status == Status.SEARCHING else t.status).value,
                "message": t.message,
                "approved": t.approved,
                "edited": t.edited,
                "query": t.custom_query,
                "queries": t.queries,
                "meta": t.meta,
                "new_name": new_filename(t.meta, t.ext) if t.meta and t.meta.get("title") else "",
                "cover_source": t.cover_source,
                "cover_file": cover_file,
                "chosen": _cand_to_dict(t.chosen) if t.chosen else None,
                "candidates": [_cand_to_dict(c) for c in t.candidates],
            }
        # drafts for files not in this scan (e.g. filtered) are kept as-is
        scanned = {self._key(t) for t in tracks}
        for k, e in old.items():
            if k not in scanned and k not in out:
                out[k] = e
                if e.get("cover_file"):
                    keep_covers.add(e["cover_file"])
        for p in self.covers.glob("*.jpg"):
            if p.name not in keep_covers:
                p.unlink(missing_ok=True)
                self._written.pop(p.name, None)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"folder": str(self.folder), "drafts": out},
                                  indent=1, ensure_ascii=False))
        tmp.replace(self.file)

    def cover_path(self, track: Track) -> Path | None:
        e = self._read().get("drafts", {}).get(self._key(track))
        if e and e.get("cover_file"):
            p = self.covers / e["cover_file"]
            return p if p.exists() else None
        return None

    def discard(self) -> None:
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)
