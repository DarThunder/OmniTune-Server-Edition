# OmniTune - Personal Offline Music Server
# Copyright (C) 2026-Present  DarThunder
# SPDX-License-Identifier: AGPL-3.0-or-later.

from dataclasses import dataclass
import difflib
import hashlib
import json
import logging
import re
import threading
import time
from typing import Dict, List, Optional, Tuple
import unicodedata

import requests

from core.configs import OMNITUNE_CONTACT
from core.database import redis_client

log = logging.getLogger("omnicast.freematcher")

STRONG = 1.0
MEDIUM = 0.7
WEAK = 0.3

MIN_TITLE_SIM = 0.75
MIN_ARTIST_SIM = 0.6
MIN_CONFIDENCE = 0.75
EARLY_EXIT = 0.93

MAX_DURATION_DIFF = 15
BLIND_MAX_DURATION_DIFF = 5
BLIND_MIN_TITLE_SIM = 0.90
VERSION_PENALTY = 0.30

RESULTS_PER_QUERY = 10
MAX_QUERIES = 5

RESULTS_PER_QUERY = 10
HTTP_TIMEOUT = 10
CACHE_TTL_HIT = 30 * 24 * 3600
CACHE_TTL_MISS = 24 * 3600
CACHE_VERSION = "v2"

USER_AGENT = f"OmniTune/1.0 ( {OMNITUNE_CONTACT} )"

_NOISE = (
    r"official|oficial|video|v[ií]deo|audio|lyrics?|letras?|lyric|music|m[uú]sica|"
    r"remaster(?:ed)?|visuali[sz]er|4k|hd|hq|mv|sub(?:s|titulad[oa]|t[ií]tulos)?|"
    r"espa[nñ]ol|traducid[ao]|explicit"
)

_BRACKET_NOISE = re.compile(
    r"(?i)\s*[\(\[\{][^\)\]\}]*\b(?:" + _NOISE + r")\b[^\)\]\}]*[\)\]\}]"
)
_PIPE_TAIL = re.compile(r"(?i)\s*[|｜/]\s*[^|｜/]*\b(?:" + _NOISE + r")\b.*$")
_DASH_TAIL = re.compile(
    r"(?i)\s+[-–—]\s*(?:(?:official|oficial)\b.*|lyrics?\b.*|letra\b.*|"
    r"(?:\d{4}\s+)?remaster(?:ed)?\b.*|visuali[sz]er\b.*)$"
)
_TRAIL_WORDS = re.compile(
    r"(?i)\s+(?:lyrics?|letra|official\s+(?:music\s+)?video|video\s+oficial|"
    r"official\s+audio|audio\s+oficial|lyric\s+video)\s*$"
)
_FEAT_BRACKET = re.compile(r"(?i)\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s[^\)\]]*[\)\]]")
_FEAT_TAIL = re.compile(r"(?i)\s+(?:feat\.?|ft\.?|featuring)\s+.*?(?=\s[-–—]\s|$)")

_ARTIST_SPLIT = re.compile(r"\s*(?:,|;|&|/|\bx\b|\by\b|\band\b|\bfeat\.?|\bft\.?|\bcon\b)\s*", re.I)
_TOPIC_SUFFIX = re.compile(r"(?i)\s*-\s*topic\s*$")
_UPLOADER_NOISE = re.compile(r"(?i)\b(?:vevo|official|oficial|music|topic)\b")

_VERSION_PATTERNS = {
    "slowed": r"slowed",
    "sped": r"sped\s*up|speed\s*up|speedup",
    "reverb": r"reverb",
    "nightcore": r"nightcore",
    "remix": r"remix|rmx|bootleg|\bflip\b",
    "live": r"\blive\b|en\s+vivo",
    "acoustic": r"acoustic|ac[uú]stic[oa]",
    "instrumental": r"instrumental|karaoke",
    "cover": r"\bcover\b",
    "8d": r"\b8d\b",
}
_VERSION_RE = {k: re.compile(v, re.I) for k, v in _VERSION_PATTERNS.items()}

_GENERIC_TOKENS = {"the", "los", "las", "el", "la", "dj", "mc", "de", "del"}

def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))

def _norm(text: str) -> str:
    text = _strip_accents(text or "").lower().replace("&", " and ")
    text = re.sub(r"['’`´]", "", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())

def _clean_title(text: str) -> str:
    t = text or ""
    t = _BRACKET_NOISE.sub("", t)
    t = _PIPE_TAIL.sub("", t)
    t = _DASH_TAIL.sub("", t)
    t = _TRAIL_WORDS.sub("", t)
    t = _FEAT_BRACKET.sub("", t)
    t = _FEAT_TAIL.sub("", t)
    return " ".join(t.split()).strip(" -–—|")

def _version_tags(text: str) -> frozenset:
    return frozenset(k for k, rx in _VERSION_RE.items() if rx.search(text or ""))

def _title_sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    jaccard = len(ta & tb) / len(ta | tb)
    return 0.5 * ratio + 0.5 * jaccard

def _artist_sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    shorter = ta if len(a) <= len(b) else tb
    longer = tb if shorter is ta else ta
    if shorter <= longer and len(" ".join(shorter)) >= 4 and not (shorter <= _GENERIC_TOKENS):
        return 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()

def _split_artists(text: Optional[str]) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    names = [text]
    for part in _ARTIST_SPLIT.split(text):
        part = part.strip()
        if part and part.lower() not in (n.lower() for n in names):
            names.append(part)
    return names

def _parse_autogen_description(description: Optional[str]) -> Tuple[Optional[str], List[str]]:
    if not description or "Provided to YouTube by" not in description:
        return None, []
    for line in (ln.strip() for ln in description.splitlines()):
        if "·" in line:
            parts = [p.strip() for p in line.split("·") if p.strip()]
            if len(parts) >= 2:
                return parts[0], parts[1:]
    return None, []

@dataclass(frozen=True)
class Hint:
    name: str
    weight: float

def _base_hints(uploader: str, artist: Optional[str], description: Optional[str]) -> List[Hint]:
    hints: List[Hint] = []

    def add(text: Optional[str], weight: float):
        for name in _split_artists(text):
            h = Hint(name, weight)
            if h not in hints:
                hints.append(h)

    add(artist, STRONG)

    _, desc_artists = _parse_autogen_description(description)
    for a in desc_artists:
        add(a, STRONG)

    uploader = uploader or ""
    if _TOPIC_SUFFIX.search(uploader):
        add(_TOPIC_SUFFIX.sub("", uploader), STRONG)
    else:
        add(" ".join(_UPLOADER_NOISE.sub("", uploader).split()), WEAK)

    return hints

def _interpretations(title: str, track: Optional[str], hints: List[Hint]) -> List[Tuple[str, List[Hint]]]:
    if track:
        return [(_clean_title(track), hints)]

    cleaned = _clean_title(title)
    whole = (cleaned, hints)
    sides = re.split(r"\s+[-–—]\s+", cleaned, maxsplit=1)
    if len(sides) != 2 or not all(sides):
       return [whole]

    left, right = sides
    as_artist_title = (right, hints + [Hint(n, MEDIUM) for n in _split_artists(left)])
    as_title_artist = (left, hints + [Hint(n, MEDIUM) for n in _split_artists(right)])
    has_strong = any(h.weight >= STRONG for h in hints)

    return [whole, as_artist_title, as_title_artist] if has_strong else [as_artist_title, as_title_artist, whole]

def _score(
        item: Dict,
        title: str,
        hints: List[Hint],
        yt_tags: frozenset,
        duration: Optional[float],
        album: Optional[str],
    ) -> Optional[float]:
        sp_title = item.get("name") or ""
        sp_artists = [a.get("name", "") for a in item.get("artists", [])]

        t_sim = _title_sim(title, _clean_title(sp_title))
        if t_sim < MIN_TITLE_SIM:
            return None

        decisive = [h for h in hints if h.weight >= MEDIUM]
        weak = [h for h in hints if h.weight < MEDIUM]
        artist: Optional[float] = None
        if decisive:
            artist = max((_artist_sim(h.name, s) for h in decisive for s in sp_artists), default=0.0)
            if artist < MIN_ARTIST_SIM:
                return None
        elif weak:
            w = max((_artist_sim(h.name, s) for h in weak for s in sp_artists), default=0.0)
            if w >= 0.8:
                artist = w

        diff: Optional[float] = None
        d_score: Optional[float] = None
        if duration:
            diff = abs((item.get("duration_ms") or 0) / 1000 - duration)
            if diff > MAX_DURATION_DIFF:
                return None
            d_score = 1.0 if diff <= 2 else 0.85 if diff <= 5 else 0.5 if diff <= 10 else 0.2

        if artist is None:
            if diff is None or diff > BLIND_MAX_DURATION_DIFF or t_sim < BLIND_MIN_TITLE_SIM:
                return None

        parts = [(t_sim, 0.45)]
        if artist is not None:
            parts.append((artist, 0.30))
        if d_score is not None:
            parts.append((d_score, 0.25))
        score = sum(v * w for v, w in parts) / sum(w for _, w in parts)

        if yt_tags != _version_tags(sp_title):
            score -= VERSION_PENALTY
        sp_album = (item.get("album") or {}).get("name") or ""
        if album and sp_album and _title_sim(album, sp_album) >= 0.85:
            score += 0.05

        return min(score, 1.0)

_COVER_ART = re.compile(r"(?i)\bcover\s+(?:audio|art)\b")
_PIPE = re.compile(r"\s*[|｜]+\s*")
SHORT_TITLE_TOKENS = 2

def _prep_title(title: str) -> str:
    first = _PIPE.split(title or "", maxsplit=1)[0].strip()
    return first or (title or "")

def _artist_evidence(item: Dict, interps) -> bool:
    names = [a.get("name", "") for a in item.get("artists", [])]
    for _, hints in interps:
        for h in hints:
            need = MIN_ARTIST_SIM if h.weight >= MEDIUM else 0.8
            if any(_artist_sim(h.name, n) >= need for n in names):
                return True
    return False

def _item(id_, title, artists, duration_s, album, cover, **extra) -> Dict:
    return {
        "id": id_,
        "name": title or "",
        "artists": [{"name": a} for a in artists if a],
        "duration_ms": int((duration_s or 0) * 1000),
        "album": {"name": album, "images": [{"url": cover}] if cover else []},
        **extra,
    }

class _RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0
 
    def wait(self) -> None:
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()

class _Provider:
    name = "base"
 
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
 
    def _get_json(self, url: str, params: dict, retries: int = 0) -> Optional[dict]:
        for attempt in range(retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=HTTP_TIMEOUT)
            except requests.RequestException as e:
                log.warning("%s: error de red: %s", self.name, e)
                return None
            if resp.status_code in (429, 503) and attempt < retries:
                time.sleep(2.5)
                continue
            break
        if resp.status_code != 200:
            log.warning("%s: HTTP %s", self.name, resp.status_code)
            return None
        try:
            return resp.json()
        except ValueError:
            log.warning("%s: respuesta no es JSON", self.name)
            return None
 
    def search(self, q: str) -> List[Dict]:
        raise NotImplementedError
 
    def cover_url(self, item: Dict) -> Optional[str]:
        images = (item.get("album") or {}).get("images") or []
        return images[0]["url"] if images else None

class DeezerProvider(_Provider):
    name = "deezer"
    URL = "https://api.deezer.com/search"
 
    def search(self, q: str) -> List[Dict]:
        data = self._get_json(self.URL, {"q": q, "limit": RESULTS_PER_QUERY})
        out = []
        for r in (data or {}).get("data") or []:
            album = r.get("album") or {}
            cover = album.get("cover_xl") or album.get("cover_big") or album.get("cover")
            out.append(_item(
                f"dz:{r.get('id')}",
                r.get("title"),
                [(r.get("artist") or {}).get("name")],
                r.get("duration"),
                album.get("title"),
                cover,
            ))
        return out

class ITunesProvider(_Provider):
    name = "itunes"
    URL = "https://itunes.apple.com/search"
 
    def search(self, q: str) -> List[Dict]:
        data = self._get_json(
            self.URL,
            {"term": q, "media": "music", "entity": "song", "limit": RESULTS_PER_QUERY},
        )
        out = []
        for r in (data or {}).get("results") or []:
            art = r.get("artworkUrl100") or ""
            cover = art.replace("100x100bb", "1200x1200bb") if art else None
            out.append(_item(
                f"it:{r.get('trackId')}",
                r.get("trackName"),
                [r.get("artistName")],
                (r.get("trackTimeMillis") or 0) / 1000,
                r.get("collectionName"),
                cover,
            ))
        return out

class MusicBrainzProvider(_Provider):
    name = "musicbrainz"
    URL = "https://musicbrainz.org/ws/2/recording"
    CAA = "https://coverartarchive.org/release/{}/front-1200"
    _limiter = _RateLimiter(1.1)
 
    def search(self, q: str) -> List[Dict]:
        self._limiter.wait()
        q = q.replace('"', " ")
        data = self._get_json(
            self.URL,
            {"query": q, "fmt": "json", "limit": RESULTS_PER_QUERY},
            retries=1,
        )
        out = []
        for r in (data or {}).get("recordings") or []:
            artists = [c.get("name") for c in r.get("artist-credit") or [] if isinstance(c, dict)]
            releases = r.get("releases") or []
            out.append(_item(
                f"mb:{r.get('id')}",
                r.get("title"),
                artists,
                (r.get("length") or 0) / 1000,
                releases[0].get("title") if releases else None,
                None,
                _releases=[rel.get("id") for rel in releases if rel.get("id")],
            ))
        return out
 
    def cover_url(self, item: Dict) -> Optional[str]:
        for rid in (item.get("_releases") or [])[:5]:
            url = self.CAA.format(rid)
            try:
                r = self.session.head(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
            except requests.RequestException:
                continue
            if r.status_code == 200:
                return url
        return None

class FreeMatcher:
    client_id = "free"
 
    def __init__(self, providers: Optional[List[_Provider]] = None):
        self.providers = providers or [
            DeezerProvider(),
            ITunesProvider(),
            MusicBrainzProvider(),
        ]

    @staticmethod
    def _cache_key(*parts) -> str:
        raw = "|".join(_norm(str(p)) if p is not None else "" for p in parts)
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        return f"meta:{CACHE_VERSION}:{digest}"
 
    @staticmethod
    def _cache_get(key: str):
        try:
            raw = redis_client.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception:
            log.debug("Caché no disponible (get)", exc_info=True)
            return None
 
    @staticmethod
    def _cache_set(key: str, result: Optional[Dict]) -> None:
        ttl = CACHE_TTL_HIT if result else CACHE_TTL_MISS
        try:
            redis_client.set(key, json.dumps({"r": result}, ensure_ascii=False), ex=ttl)
        except Exception:
            log.debug("Caché no disponible (set)", exc_info=True)
 
    @staticmethod
    def _queries(interps) -> List[str]:
        precise: List[str] = []
        loose: List[str] = []
 
        def add(bucket: List[str], q: str):
            q = " ".join(q.replace('"', "").split())
            if q and q not in precise and q not in loose:
                bucket.append(q)
 
        for title, hints in interps:
            if not title:
                continue
            decisive = [h for h in hints if h.weight >= 0.7]
            if decisive:
                add(precise, f"{decisive[0].name} {title}")
                add(loose, title)
            else:
                weak = hints[0].name if hints else ""
                add(precise, f"{weak} {title}")
                add(loose, title)
 
        return (precise + loose)[:MAX_QUERIES]
 
    def find_metadata(
        self,
        yt_title: str,
        yt_uploader: str = "",
        *,
        duration: Optional[float] = None,
        artist: Optional[str] = None,
        track: Optional[str] = None,
        album: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Optional[Dict]:
        key = self._cache_key(
            yt_title, yt_uploader, round(duration) if duration else "", artist, track
        )
        cached = self._cache_get(key)
        if cached is not None:
            return cached.get("r")
 
        result = self._search_all(
            yt_title, yt_uploader, duration, artist, track, album, description
        )
        self._cache_set(key, result)
        return result
 
    @staticmethod
    def _build_result(provider, item, score, relaxed=False) -> Dict:
        result = {
            "title": item["name"],
            "uploader": ", ".join(a["name"] for a in item["artists"]),
            "cover_url": provider.cover_url(item),
            "album": (item.get("album") or {}).get("name"),
            "source": provider.name,
            "source_id": item["id"],
            "confidence": round(score, 3),
        }
        if relaxed:
            result["duration_relaxed"] = True
        return result
 
    def _search_all(self, yt_title, yt_uploader, duration, artist, track, album, description):
        yt_title = _prep_title(yt_title)
        desc_track, _ = _parse_autogen_description(description)
        track = track or desc_track
 
        hints = _base_hints(yt_uploader, artist, description)
        interps = _interpretations(yt_title, track, hints)
        if len(interps) > 1 and not any(h.weight >= STRONG for h in hints):
            interps = interps[:-1]
        yt_tags = _version_tags(_COVER_ART.sub("", f"{yt_title} {track or ''}"))
        queries = self._queries(interps)
 
        pool = []
        for provider in self.providers:
            best_score, best_item = 0.0, None
            seen = set()
            for q in queries:
                for item in provider.search(q):
                    item_id = item.get("id")
                    if not item_id or item_id in seen:
                        continue
                    seen.add(item_id)
                    pool.append((provider, item))
                    short = len(_norm(item.get("name", "")).split()) <= SHORT_TITLE_TOKENS
                    for title, h in interps:
                        s = _score(item, title, h, yt_tags, duration, album)
                        if s is None or s <= best_score:
                            continue
                        if short and not _artist_evidence(item, interps):
                            continue
                        best_score, best_item = s, item
                if best_score >= EARLY_EXIT:
                    break
 
            if best_item and best_score >= MIN_CONFIDENCE:
                result = self._build_result(provider, best_item, best_score)
                log.info(
                    "%s: %r -> %s - %s (%.2f)",
                    provider.name, yt_title, result["uploader"], result["title"], best_score,
                )
                return result
            log.info("%s: sin match confiable para %r (mejor=%.2f)", provider.name, yt_title, best_score)
 
        if duration:
            best_score, best = 0.0, None
            for provider, item in pool:
                for title, h in interps:
                    s = _score(item, title, h, yt_tags, None, album)
                    if s is not None and s > best_score:
                        best_score, best = s, (provider, item)
            if best and best_score >= MIN_CONFIDENCE:
                provider, item = best
                result = self._build_result(provider, item, best_score, relaxed=True)
                log.info(
                    "%s (sin duración): %r -> %s - %s (%.2f)",
                    provider.name, yt_title, result["uploader"], result["title"], best_score,
                )
                return result
 
        return None