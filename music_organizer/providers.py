"""Free, key-less metadata sources.

* JioSaavn public web API  - best coverage for Hindi / Bengali / Indian music
* iTunes Search API        - huge catalogue, good for English & Bollywood
* MusicBrainz + Cover Art Archive - open database, used as a fallback

All of them are queried with plain HTTPS GETs; no account or key is needed.
"""

from __future__ import annotations

import html
import re
import threading
import time

import httpx

from .models import Candidate

UA = "MusicOrganizer/0.1 (open-source music tagger; https://github.com/)"

_client = httpx.Client(
    timeout=httpx.Timeout(15.0, connect=10.0),
    headers={"User-Agent": UA, "Accept": "application/json"},
    follow_redirects=True,
)


def _get_json(url: str, params: dict | None = None, retries: int = 3):
    delay = 1.0
    for attempt in range(retries):
        try:
            r = _client.get(url, params=params)
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError("busy", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError):
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay *= 2
    return None


def download(url: str) -> bytes | None:
    try:
        r = _client.get(url, headers={"Accept": "image/*"})
        if r.status_code == 200 and len(r.content) > 1000:
            return r.content
    except httpx.HTTPError:
        pass
    return None


def _u(s) -> str:
    return html.unescape(str(s or "")).strip()


# --------------------------------------------------------------------------- #
# JioSaavn
# --------------------------------------------------------------------------- #

SAAVN = "https://www.jiosaavn.com/api.php"


def saavn_search(query: str, limit: int = 10) -> list[Candidate]:
    data = _get_json(SAAVN, {
        "__call": "search.getResults", "_format": "json", "_marker": "0",
        "api_version": "4", "ctx": "web6dot0", "n": str(limit), "p": "1",
        "q": query,
    })
    out = []
    for rank, r in enumerate((data or {}).get("results", []) or []):
        mi = r.get("more_info") or {}
        amap = mi.get("artistMap") or {}
        primary = [_u(a.get("name")) for a in amap.get("primary_artists") or []]
        roles: dict[str, set] = {}
        for a in amap.get("artists") or []:
            roles.setdefault(_u(a.get("name")), set()).add(a.get("role"))
        # Saavn puts the music director / lyricist in "primary artists" for
        # film songs; the person you hear is the singer.
        singers = [n for n, rs in roles.items() if "singer" in rs]
        if singers:
            singers.sort(key=lambda n: primary.index(n) if n in primary else 99)
            # composer credited as a "singer" next to the real vocalists
            pure = [n for n in singers if "music" not in roles[n]]
            artists = pure or singers
        else:
            artists = [n for n in primary
                       if not (roles.get(n) and roles[n] <= {"music", "lyricist"})] or primary
        if not artists:
            artists = [_u(a) for a in re.split(r",\s*", _u(mi.get("music"))) if a]
        composers = [_u(a.get("name")) for a in amap.get("artists") or []
                     if a.get("role") == "music"]
        lang = _u(r.get("language")).title()
        try:
            dur = float(mi.get("duration") or 0) or None
        except ValueError:
            dur = None
        img = _u(r.get("image"))
        img = re.sub(r"-\d+x\d+\.(jpg|png|webp)$", r"-500x500.\1", img)
        out.append(Candidate(
            source="jiosaavn",
            title=_u(r.get("title")),
            artists=[a for a in artists if a],
            album=_u(mi.get("album")),
            year=_u(r.get("year"))[:4],
            genre=lang,
            composer=", ".join(dict.fromkeys(composers)),
            label=_u(mi.get("label")),
            duration=dur,
            art_url=img,
            rank=rank,
        ))
    return out


# --------------------------------------------------------------------------- #
# iTunes
# --------------------------------------------------------------------------- #

ITUNES = "https://itunes.apple.com/search"
_SPLIT_ARTISTS = re.compile(r"\s*(?:,|&| and | feat\.? | ft\.? | x )\s*", re.I)


def itunes_search(query: str, country: str = "IN", limit: int = 15) -> list[Candidate]:
    data = _get_json(ITUNES, {
        "term": query, "entity": "song", "media": "music",
        "limit": str(limit), "country": country,
    })
    out = []
    for rank, r in enumerate((data or {}).get("results", []) or []):
        if r.get("wrapperType") != "track":
            continue
        art = _u(r.get("artworkUrl100"))
        art = re.sub(r"/\d+x\d+bb\.(jpg|png)$", r"/1000x1000bb.jpg", art)
        ms = r.get("trackTimeMillis")
        album = _u(r.get("collectionName"))
        album = re.sub(r"\s*-\s*(Single|EP)$", "", album)
        out.append(Candidate(
            source="itunes",
            title=_u(r.get("trackName")),
            artists=[a for a in _SPLIT_ARTISTS.split(_u(r.get("artistName"))) if a],
            album=album,
            year=_u(r.get("releaseDate"))[:4],
            genre=_u(r.get("primaryGenreName")),
            duration=ms / 1000 if ms else None,
            art_url=art,
            rank=rank,
        ))
    return out


# --------------------------------------------------------------------------- #
# MusicBrainz (strict 1 request / second policy)
# --------------------------------------------------------------------------- #

MB = "https://musicbrainz.org/ws/2/recording"
_mb_lock = threading.Lock()
_mb_last = [0.0]


def _mb_escape(s: str) -> str:
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", s)


def musicbrainz_search(title: str, artist: str = "", limit: int = 8) -> list[Candidate]:
    q = f'recording:"{_mb_escape(title)}"'
    if artist:
        q += f' AND artist:"{_mb_escape(artist)}"'
    with _mb_lock:
        wait = 1.1 - (time.monotonic() - _mb_last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            data = _get_json(MB, {"query": q, "fmt": "json", "limit": str(limit)})
        finally:
            _mb_last[0] = time.monotonic()
    out = []
    for rank, r in enumerate((data or {}).get("recordings", []) or []):
        artists = [_u(c.get("name")) for c in r.get("artist-credit") or [] if c.get("name")]
        releases = sorted(
            (rel for rel in r.get("releases") or []),
            key=lambda rel: rel.get("date") or "9999",
        )
        rel = releases[0] if releases else {}
        out.append(Candidate(
            source="musicbrainz",
            title=_u(r.get("title")),
            artists=artists,
            album=_u(rel.get("title")),
            year=_u(r.get("first-release-date") or rel.get("date"))[:4],
            duration=(r.get("length") or 0) / 1000 or None,
            art_url=(f"https://coverartarchive.org/release/{rel['id']}/front-500"
                     if rel.get("id") else ""),
            rank=rank,
        ))
    return out
