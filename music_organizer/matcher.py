"""Turn a Track into search queries, score every candidate, pick the best one."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from rapidfuzz import fuzz

from . import providers
from .models import Candidate, Status, Track
from .romanize import clean_album, clean_title, is_latin, split_featuring, to_english
from .scanner import clean_text, filename_parts

MATCH_THRESHOLD = 75
REVIEW_THRESHOLD = 45

_VERSION_WORDS = (
    "remix", "lofi", "lo-fi", "lo fi", "cover", "live", "unplugged", "instrumental",
    "karaoke", "reprise", "mashup", "slowed", "reverb", "sped up", "recreated",
    "acoustic", "female version", "male version", "sad version", "dj ", "8d",
    "revisited", "rendition", "remake", "version", "sufi", "(sad)", "sad ", "male)",
    "female)", "duet", "orchestral", "chill", "bass boosted", "remastered", " mix)",
    " mix]", " edit)", "radio edit", "extended",
)
_COMPILATION_WORDS = (
    "hits", "best of", "collection", "nonstop", "non-stop", "jukebox", "top ",
    "greatest", "essentials", "playlist", "mashup", "romantic songs", "evergreen",
    "special", "antakshari", "vol.", "volume", "songs of", "party", "love songs",
    "superhits", "golden", "classics", "anthology", "compilation", "various",
)
_PUNCT = re.compile(r"[^\w\s]")
_BRACKET = re.compile(r"[\(\[][^\)\]]*[\)\]]")


def _norm(s: str) -> str:
    s = to_english(s).lower()
    s = re.sub(r"\b(feat|ft)\.?\b.*$", "", s)
    s = _PUNCT.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _nobr(s: str) -> str:
    return _norm(_BRACKET.sub(" ", s)) or _norm(s)


def _valid_tag(s: str) -> str:
    s = clean_text(to_english(s))
    if not s or re.fullmatch(r"(track\s*)?\d*|unknown.*|untitled.*", s, re.I):
        return ""
    return s


class Hints:
    """Everything we know about a file before going online."""

    def __init__(self, track: Track):
        self.title = _valid_tag(track.cur_title)
        self.artist = _valid_tag(track.cur_artist)
        self.album = _valid_tag(track.cur_album)
        self.duration = track.duration

        if track.custom_query:
            stem = clean_text(track.custom_query)
            parts = [p.strip() for p in re.split(r"\s+-\s+", stem) if p.strip()]
            self.title = self.artist = ""
        else:
            stem, parts = filename_parts(track.path)
        self.stem = stem
        self.parts = parts or ([stem] if stem else [])

        self.title_hints = [h for h in [self.title, *self.parts] if h]
        self.artist_hints = [h for h in [self.artist, *self.parts[1:], *self.parts[:1]] if h]
        if self.title and not self.artist:
            self.artist_hints = self.parts
        self.blob = _norm(" ".join([self.title, self.artist, self.album, stem]))

    def queries(self) -> list[str]:
        qs = []
        if self.title and self.artist:
            qs.append(f"{self.title} {self.artist}")
        if self.stem:
            qs.append(self.stem.replace(" - ", " "))
        if self.title and self.album:
            qs.append(f"{self.title} {self.album}")
        if self.title:
            qs.append(self.title)
        if len(self.parts) > 1:
            qs.append(self.parts[0])
        # non-Latin filenames/tags: also search the romanized spelling
        qs += [to_english(q) for q in qs if not is_latin(q)]
        seen, out = set(), []
        for q in qs:
            k = _norm(q)
            if k and k not in seen:
                seen.add(k)
                out.append(q)
        return out


def score(c: Candidate, h: Hints) -> float:
    ct, ct_nb = _norm(c.title), _nobr(c.title)
    if not ct:
        return 0.0
    t = max((max(fuzz.token_sort_ratio(ct, _norm(x)), fuzz.ratio(ct_nb, _nobr(x)))
             for x in h.title_hints), default=0)

    cand_artists = [_norm(a) for a in c.artists if a] or [""]
    a = None
    if h.artist_hints:
        a = max(fuzz.token_set_ratio(ca, _norm(x))
                for ca in cand_artists for x in h.artist_hints)
    combo = fuzz.token_set_ratio(f"{ct} {' '.join(cand_artists)}", _norm(h.stem or h.title))

    if h.title and h.artist:
        base = 0.6 * t + 0.3 * (a or 0) + 0.1 * combo
    elif a is not None and len(h.parts) > 1:
        base = 0.55 * t + 0.25 * a + 0.2 * combo
    else:
        base = 0.65 * t + 0.35 * combo

    if c.duration and h.duration:
        d = abs(c.duration - h.duration)
        base += 8 if d <= 3 else 3 if d <= 10 else -15 if d > 30 else 0
        if d > 60:
            base -= 10

    hay_t = f" {c.title.lower()} "
    hay_al = f" {c.album.lower()} "
    for w in _VERSION_WORDS:
        if w in hay_t and w not in f" {h.blob} ":
            base -= 20
            break
    for w in _COMPILATION_WORDS:
        if w in hay_al and w not in f" {h.blob} ":
            base -= 6
            break
    extra = _norm(" ".join(_BRACKET.findall(split_featuring(c.title)[0])))
    if extra and not re.search(r"\bfrom\b", extra) and extra not in h.blob:
        base -= 6
    if re.search(r"\(\s*from\s", c.title, re.I):
        base -= 3
    if not is_latin(c.title) or not all(is_latin(x) for x in c.artists):
        base -= 3
    if not c.year:
        base -= 3
    # the provider's own popularity ordering is a decent tie-breaker
    base += max(0, 6 - c.rank)
    return max(0.0, base)


def _overlap(a: Candidate, b: Candidate) -> bool:
    x = {_norm(n) for n in a.artists}
    return any(_norm(n) in x for n in b.artists)


def _consensus(cands: list[Candidate]) -> None:
    """Reward versions that several sources agree on (the original, usually)."""
    for c in cands:
        agree = sum(1 for o in cands if o is not c and o.source != c.source
                    and _nobr(o.title) == _nobr(c.title) and _overlap(o, c))
        same = sum(1 for o in cands if o is not c and o.source == c.source
                   and _nobr(o.title) == _nobr(c.title) and _overlap(o, c))
        c.agree = agree
        c.score += min(8, 4 * agree) + min(2, same)


def _search_round(query: str) -> list[Candidate]:
    jobs = [
        (providers.saavn_search, (query,)),
        (providers.itunes_search, (query, "IN")),
        (providers.itunes_search, (query, "US")),
    ]
    out: list[Candidate] = []
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = [ex.submit(fn, *args) for fn, args in jobs]
        for f in futs:
            try:
                out.extend(f.result())
            except Exception:
                pass
    return out


def _dedupe(cands: list[Candidate]) -> list[Candidate]:
    seen, out = set(), []
    for c in cands:
        k = (c.source, _norm(c.title), _norm(c.artist), _norm(c.album), c.year)
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def pick(cands: list[Candidate]) -> Candidate | None:
    """Best score wins; the year is the median of the strongly agreeing
    releases, which shrugs off both re-releases and bad catalogue dates."""
    if not cands:
        return None
    ranked = sorted(cands, key=lambda c: c.score, reverse=True)
    best = ranked[0]
    close = [c for c in ranked if best.score - c.score <= 6 and c.year
             and _nobr(c.title) == _nobr(best.title) and _overlap(c, best)]
    if len(close) < 2:
        return best
    years = sorted(c.year for c in close)
    year = years[(len(years) - 1) // 2]
    return max((c for c in close if c.year == year), key=lambda c: c.score)


def lookup(track: Track, log=lambda *_: None) -> None:
    """Search every provider and fill track.candidates / chosen / status."""
    h = Hints(track)
    found: list[Candidate] = []
    for q in h.queries()[:5]:
        try:
            found.extend(_search_round(q))
        except Exception as e:
            log(f"search failed for {q!r}: {e}")
        found = _dedupe(found)
        for c in found:
            c.score = score(c, h)
        _consensus(found)
        if found and max(c.score for c in found) >= 95:
            break

    if not found or max(c.score for c in found) < MATCH_THRESHOLD:
        title = h.title or (h.parts[0] if h.parts else h.stem)
        artist = h.artist or (h.parts[1] if len(h.parts) > 1 else "")
        if title:
            try:
                mb = providers.musicbrainz_search(title, artist)
                found = _dedupe(found + mb)
                for c in found:
                    c.score = score(c, h)
                _consensus(found)
            except Exception as e:
                log(f"MusicBrainz failed: {e}")

    found.sort(key=lambda c: c.score, reverse=True)
    track.candidates = found[:25]
    best = pick(found)

    if best and best.score >= REVIEW_THRESHOLD:
        track.chosen = best
        track.status = Status.MATCHED if best.score >= MATCH_THRESHOLD else Status.REVIEW
        # Only a song title to go on? Lots of songs share a title - unless the
        # length matches or several sources agree, ask the user to check.
        title_only = not h.artist and len(h.parts) < 2
        dur_ok = bool(best.duration and h.duration and abs(best.duration - h.duration) <= 3)
        if title_only and not dur_ok and best.agree == 0:
            track.status = Status.REVIEW
        track.message = f"{best.source} {min(100, best.score):.0f}%"
    elif h.title and h.artist:
        # Nothing online, but the file's own tags look sane: tidy those up.
        track.chosen = Candidate(source="file tags", title=h.title, artists=[h.artist],
                                 album=h.album, year=track.cur_year)
        track.status = Status.REVIEW
        track.message = "not found online - using cleaned file tags"
    else:
        track.chosen = None
        track.status = Status.NOT_FOUND
        track.message = "no match - press / to search, e to fill in by hand"

    track.meta = final_meta(track.chosen) if track.chosen else None
    track.approved = False
    track.edited = False


def _clean_artists(names: list[str]) -> list[str]:
    """Romanize, de-duplicate and drop combined entries like 'A & B'."""
    out: list[str] = []
    for n in names:
        n = to_english(n)
        if n and n.lower() not in (x.lower() for x in out):
            out.append(n)
    lower = {x.lower() for x in out}
    def combo(n):
        parts = [p.lower() for p in re.split(r"\s*(?:&|,| and )\s*", n) if p]
        return len(parts) > 1 and any(p in lower for p in parts)
    return [n for n in out if not combo(n)]


def blank_meta(track: Track) -> dict:
    """Starting point for filling in a song by hand."""
    h = Hints(track)
    title = h.title or (h.parts[0] if h.parts else h.stem)
    artist = h.artist or (h.parts[1] if len(h.parts) > 1 else "")
    return final_meta(Candidate(source="manual", title=title,
                                artists=[artist] if artist else [],
                                album=h.album, year=track.cur_year))


def final_meta(c: Candidate) -> dict:
    """The exact, English-only values that will be written."""
    title, feats = split_featuring(c.title)
    artists = _clean_artists(c.artists + feats)
    composer = to_english(c.composer)
    return {
        "title": clean_title(title),
        "artist": ", ".join(artists),
        "artists": artists,
        "album": clean_album(c.album),
        # film soundtracks group best under the music director
        "albumartist": (composer.split(", ")[0] if composer and c.source == "jiosaavn"
                        else (artists[0] if artists else "")),
        "year": c.year[:4] if c.year and c.year[:4].isdigit() else "",
        "genre": to_english(c.genre),
        "composer": composer,
        "label": to_english(c.label),
    }
