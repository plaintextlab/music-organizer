"""Plain data containers shared by every module."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Status(str, Enum):
    PENDING = "pending"      # scanned, not looked up yet
    SEARCHING = "searching"
    MATCHED = "matched"      # confident match
    REVIEW = "review"        # weak match - check it before applying
    NOT_FOUND = "not found"
    DONE = "done"            # tags written + renamed
    ERROR = "error"


@dataclass
class Candidate:
    source: str
    title: str
    artists: list[str]
    album: str = ""
    year: str = ""
    genre: str = ""
    composer: str = ""
    label: str = ""
    duration: float | None = None
    art_url: str = ""
    rank: int = 0          # position in the provider's own result list
    agree: int = 0         # other sources that list the same song + artist
    score: float = 0.0

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)


@dataclass
class Track:
    path: Path
    duration: float | None = None
    # what is in the file right now
    cur_title: str = ""
    cur_artist: str = ""
    cur_album: str = ""
    cur_year: str = ""
    has_art: bool = False
    # lookup state
    custom_query: str = ""
    queries: list[str] = field(default_factory=list)   # what the last search sent
    candidates: list[Candidate] = field(default_factory=list)
    chosen: Candidate | None = None
    status: Status = Status.PENDING
    message: str = ""
    # ---- the draft: nothing below touches the file until you confirm ----
    meta: dict | None = None     # exact values that will be written (editable)
    approved: bool = False       # you reviewed it and want it written
    edited: bool = False         # you changed something by hand
    # proposed cover: raw JPEG bytes and where it came from
    cover: bytes | None = None
    cover_source: str = ""   # "online" | "existing" | "generated" | "file"

    @property
    def ext(self) -> str:
        return self.path.suffix.lower()
