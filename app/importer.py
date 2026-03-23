"""
Core import logic — Navidrome playlist import + MusicBrainz + Lidarr pipeline.
Runs in a background thread, streams progress via a queue.
"""

import time
import hashlib
import random
import string
import requests
import threading
import json as _json
from pathlib import Path
from urllib.parse import urlencode
from datetime import datetime
from queue import Queue

from app import config as cfg
from rapidfuzz import fuzz, process as fuzz_process

FUZZY_SONG_THRESHOLD   = 75
FUZZY_ARTIST_THRESHOLD = 80
FUZZY_ALBUM_THRESHOLD  = 72

_run_lock   = threading.Lock()
_is_running = False
progress_queue: Queue = Queue()

MB_BASE_URL = "https://musicbrainz.org/ws/2"
MB_AUTH_URL = "https://musicbrainz.org/oauth2/token"


# ═════════════════════════════════════════════════════════════════
#  PROGRESS STREAMING
# ═════════════════════════════════════════════════════════════════

_emit_override = None
_stop_flag     = False
_stop_event    = None   # set by sync.py after import to share the threading.Event

def set_stop_flag(val: bool):
    global _stop_flag
    _stop_flag = val

def _should_stop() -> bool:
    return _stop_flag

def _interruptible_sleep(seconds: float):
    """Sleep in small chunks so a stop request wakes us up quickly."""
    end = time.time() + seconds
    while time.time() < end:
        if _stop_flag:
            return
        if _stop_event is not None and _stop_event.is_set():
            return
        time.sleep(min(0.25, end - time.time()))

def emit(level: str, msg: str):
    line = f"[{level}] {msg}"
    print(line)
    if _emit_override:
        _emit_override(level, msg)
    else:
        progress_queue.put({"level": level, "msg": msg, "ts": datetime.now().strftime("%H:%M:%S")})

def emit_info(msg):  emit("INFO",    msg)
def emit_warn(msg):  emit("WARN",    msg)
def emit_error(msg): emit("ERROR",   msg)
def emit_ok(msg):    emit("SUCCESS", msg)
def emit_debug(msg): emit("DEBUG",   msg)

def set_emit_override(fn):
    global _emit_override
    _emit_override = fn

def clear_emit_override():
    global _emit_override
    _emit_override = None


# ═════════════════════════════════════════════════════════════════
#  MUSICBRAINZ AUTH
# ═════════════════════════════════════════════════════════════════

class MusicBrainzAuth:
    def __init__(self):
        self._access_token = None
        self._token_expiry = 0.0

    def _fetch_token(self, conf: dict):
        mb = conf["musicbrainz"]
        emit_info("Fetching MusicBrainz access token...")
        try:
            resp = requests.post(MB_AUTH_URL, data={
                "grant_type":    "refresh_token",
                "refresh_token": mb["refresh_token"],
                "client_id":     mb["client_id"],
                "client_secret": mb["client_secret"],
            }, headers={"User-Agent": f"{mb['app_name']}/{mb['version']} ( {mb['contact']} )"}, timeout=15)
            emit_debug(f"MB token request → HTTP {resp.status_code}")
            resp.raise_for_status()
            data               = resp.json()
            self._access_token = data.get("access_token")
            expires_in         = data.get("expires_in", 3600)
            self._token_expiry = time.time() + expires_in - 60
            emit_ok(f"MusicBrainz token obtained (expires in {expires_in}s)")
        except Exception as e:
            emit_warn(f"Could not get MusicBrainz token: {e} — continuing unauthenticated")
            self._access_token = None

    def get_headers(self, conf: dict) -> dict:
        mb = conf["musicbrainz"]
        if mb.get("client_id") and mb.get("client_secret") and mb.get("refresh_token"):
            if not self._access_token or time.time() >= self._token_expiry:
                self._fetch_token(conf)
        headers = {
            "User-Agent": f"{mb['app_name']}/{mb['version']} ( {mb['contact']} )",
            "Accept":     "application/json",
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers


mb_auth       = MusicBrainzAuth()
_last_mb_call = 0.0

# ═════════════════════════════════════════════════════════════════
#  MUSICBRAINZ ID CACHE
#  Persisted to data/mb_cache.json to avoid repeat API calls.
#  Keys:
#    "artist:{artist_lower}"                    → {artist_name, artist_mb_id}
#    "release:{artist_lower}|{album_lower}"     → full mb_result dict
#    "recording:{artist_lower}|{title_lower}"   → album_name string
# ═════════════════════════════════════════════════════════════════

_MB_CACHE_PATH = Path("/data/mb_cache.json")
_mb_cache: dict = {}
_mb_cache_dirty = False


def _load_mb_cache():
    global _mb_cache
    try:
        if _MB_CACHE_PATH.exists():
            _mb_cache = _json.loads(_MB_CACHE_PATH.read_text(encoding="utf-8"))
            emit_debug(f"MB cache loaded: {len(_mb_cache)} entries")
    except Exception as e:
        emit_debug(f"MB cache load error: {e}")
        _mb_cache = {}


def _save_mb_cache():
    global _mb_cache_dirty
    if not _mb_cache_dirty:
        return
    try:
        _MB_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _MB_CACHE_PATH.write_text(
            _json.dumps(_mb_cache, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        _mb_cache_dirty = False
        emit_debug(f"MB cache saved: {len(_mb_cache)} entries")
    except Exception as e:
        emit_debug(f"MB cache save error: {e}")


def _mb_cache_get(key: str):
    return _mb_cache.get(key)


def _mb_cache_set(key: str, value):
    global _mb_cache_dirty
    _mb_cache[key] = value
    _mb_cache_dirty = True


# ═════════════════════════════════════════════════════════════════
#  MUSICBRAINZ API
# ═════════════════════════════════════════════════════════════════

def mb_get(endpoint: str, params: dict, conf: dict) -> dict:
    global _last_mb_call
    cooldown = conf["cooldowns"]["mb_cooldown"]
    elapsed  = time.time() - _last_mb_call
    if elapsed < cooldown:
        _interruptible_sleep(cooldown - elapsed)

    params["fmt"] = "json"
    headers       = mb_auth.get_headers(conf)
    url           = f"{MB_BASE_URL}/{endpoint}"
    emit_debug(f"MB GET {endpoint} params={list(params.keys())}")
    resp          = requests.get(url, params=params, headers=headers, timeout=15)
    _last_mb_call = time.time()
    emit_debug(f"MB response → HTTP {resp.status_code}")

    if resp.status_code == 503:
        emit_warn("MusicBrainz rate limited (503) — waiting 10s then retrying")
        _interruptible_sleep(10)
        if _stop_flag:
            return {}
        resp          = requests.get(url, params=params, headers=headers, timeout=15)
        _last_mb_call = time.time()
        emit_debug(f"MB retry → HTTP {resp.status_code}")

    if resp.status_code == 401 and conf["musicbrainz"].get("client_id"):
        emit_warn("MusicBrainz token expired (401) — refreshing and retrying")
        mb_auth._access_token = None
        headers               = mb_auth.get_headers(conf)
        resp                  = requests.get(url, params=params, headers=headers, timeout=15)
        _last_mb_call         = time.time()
        emit_debug(f"MB retry after refresh → HTTP {resp.status_code}")

    if not resp.ok:
        emit_error(f"MusicBrainz error {resp.status_code}: {resp.text[:200]}")
    resp.raise_for_status()
    return resp.json()


import unicodedata as _unicodedata
import re as _artist_re

_FEAT_IN_ARTIST  = _artist_re.compile(r'\s+(feat\.?|ft\.?|featuring)\s+.*$', _artist_re.IGNORECASE)
_MULTI_ARTIST_SEP = _artist_re.compile(r'\s*,\s+|\s+&\s+|\s+and\s+', _artist_re.IGNORECASE)

def _normalize_name(s: str) -> str:
    """Fold accents/diacritics for fuzzy comparison (Tiësto → Tiesto)."""
    return _unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()

def _primary_artist(artist: str) -> str:
    """Extract primary artist: strip feat. clauses and take first name in a list."""
    a = _FEAT_IN_ARTIST.sub("", artist).strip()
    a = _MULTI_ARTIST_SEP.split(a)[0].strip()
    return a


def mb_search_artist(artist_name: str, conf: dict) -> dict | None:
    """
    Search MusicBrainz for an artist.
    - Tries the full name first
    - If that returns 0 results or no confident match, retries with the
      primary artist only (strips feat. clauses and multi-artist separators)
    - Scoring uses accent-normalised strings so 'Tiësto' beats '420 Tiesto'
    """
    def _try_search(name: str) -> dict | None:
        cache_key = f"artist:{name.lower()}"
        cached = _mb_cache_get(cache_key)
        if cached:
            emit_debug(f"  MB cache hit — artist '{cached.get('name')}' MBID={cached.get('id')}")
            return cached
        emit_info(f"  MB artist search: '{name}'")
        try:
            data    = mb_get("artist", {"query": f'artist:"{name}"', "limit": "10"}, conf)
            artists = data.get("artists", [])
            emit_debug(f"  MB returned {len(artists)} artist candidate(s)")
            if not artists:
                emit_warn(f"  No artist results for '{name}'")
                return None

            norm_name = _normalize_name(name)
            scored = []
            for a in artists:
                a_name = a.get("name", "")
                # Score against both original and accent-stripped versions
                s = max(
                    fuzzy_score(name,      a_name),
                    fuzzy_score(norm_name, _normalize_name(a_name)),
                )
                scored.append((s, a))
            scored.sort(key=lambda x: x[0], reverse=True)
            best_score, best = scored[0]

            emit_debug(f"  Top candidates: {[(s, a.get('name')) for s,a in scored[:3]]}")
            if best_score >= FUZZY_ARTIST_THRESHOLD:
                emit_ok(f"  Artist matched ({best_score}%): '{best.get('name')}' MBID={best.get('id')}")
                _mb_cache_set(cache_key, best)
                return best
            emit_warn(f"  No confident match for '{name}' (best: '{best.get('name')}' "
                      f"{best_score}% — threshold {FUZZY_ARTIST_THRESHOLD}%)")
            return None
        except Exception as e:
            emit_error(f"  Artist search exception: {e}")
            return None

    # Pass 1: full artist string
    result = _try_search(artist_name)
    if result:
        return result

    # Pass 2: primary artist only (strip feat. / multi-artist)
    primary = _primary_artist(artist_name)
    if primary and primary.lower() != artist_name.lower():
        emit_info(f"  Retrying with primary artist: '{primary}'")
        result = _try_search(primary)

    return result


def mb_find_album(artist_mb_id: str, album_name: str, conf: dict) -> dict | None:
    emit_info(f"  MB fetching release-groups for artist MBID={artist_mb_id}")
    try:
        offset, limit, all_rgs = 0, 100, []
        while True:
            data = mb_get(f"artist/{artist_mb_id}",
                          {"inc": "release-groups", "limit": str(limit), "offset": str(offset)}, conf)
            rgs  = data.get("release-groups", [])
            all_rgs.extend(rgs)
            emit_debug(f"  Got {len(rgs)} release-groups (offset {offset})")
            if len(rgs) < limit:
                break
            offset += limit
            _interruptible_sleep(conf["cooldowns"]["mb_cooldown"])

        emit_info(f"  Total release-groups: {len(all_rgs)} — searching for '{album_name}'")
        titles  = [r.get("title", "") for r in all_rgs]
        results = fuzz_process.extract(album_name, titles, scorer=fuzz.token_sort_ratio, limit=5)

        best_title, best_score, best_idx = None, 0, None
        for title_str, score, idx in results:
            combined = max(score, fuzz.partial_ratio(album_name.lower(), title_str.lower()))
            if combined > best_score:
                best_score, best_title, best_idx = combined, title_str, idx

        emit_debug(f"  Top album matches: {[(s, t) for t,s,_ in results[:3]]}")
        if best_score >= FUZZY_ALBUM_THRESHOLD:
            match = all_rgs[best_idx]
            emit_ok(f"  Album matched ({best_score}%): '{match.get('title')}' MBID={match.get('id')}")
            return match
        else:
            emit_warn(f"  Album '{album_name}' not found (best: '{best_title}' {best_score}% — threshold {FUZZY_ALBUM_THRESHOLD}%)")
            emit_warn(f"  Available titles (first 10): {[r.get('title','') for r in all_rgs[:10]]}")
            return None
    except Exception as e:
        emit_error(f"  Album fetch exception: {e}")
        return None


def mb_lookup_release(artist_name: str, album_name: str, conf: dict) -> dict | None:
    release_key = f"release:{artist_name.lower()}|{album_name.lower()}"
    cached = _mb_cache_get(release_key)
    if cached:
        emit_debug(f"  MB cache hit — release '{cached.get('album_name')}' by '{cached.get('artist_name')}'")
        return cached
    artist = mb_search_artist(artist_name, conf)
    if not artist:
        return None
    artist_mb_id = artist.get("id", "")
    album        = mb_find_album(artist_mb_id, album_name, conf)
    if not album:
        return None
    result = {
        "artist_name":  artist.get("name", artist_name),
        "artist_mb_id": artist_mb_id,
        "album_name":   album.get("title", album_name),
        "album_mb_id":  album.get("id", ""),
    }
    _mb_cache_set(release_key, result)
    return result


# ═════════════════════════════════════════════════════════════════
#  NAVIDROME API
# ═════════════════════════════════════════════════════════════════

def make_token_auth(conf: dict) -> dict:
    nd   = conf["navidrome"]
    salt = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    tok  = hashlib.md5((nd["password"] + salt).encode()).hexdigest()
    return {"u": nd["user"], "t": tok, "s": salt, "v": "1.16.1", "c": "playlist-manager", "f": "json"}


def nd_get(endpoint: str, conf: dict, extra: dict = None) -> dict:
    params = {**make_token_auth(conf), **(extra or {})}
    resp   = requests.get(f"{conf['navidrome']['url']}/rest/{endpoint}", params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("subsonic-response", {})
    if data.get("status") != "ok":
        raise RuntimeError(f"Navidrome error: {data.get('error', data)}")
    return data


def fuzzy_score(a: str, b: str) -> int:
    return max(
        fuzz.token_sort_ratio(a.lower(), b.lower()),
        fuzz.partial_ratio(a.lower(), b.lower()),
    )


import re as _re

# Patterns stripped from titles before Navidrome search
_TITLE_NOISE = _re.compile(
    r'\s*[\(\[]('
    r'official\s*(music\s*)?video|official\s*audio|official\s*lyric\s*video|'
    r'lyrics?|lyric\s*video|hd\s*remastered|hd|hq|4k|visuali[sz]er|'
    r'radio\s*edit|extended\s*mix|original\s*mix|slowed\s*(\+\s*reverb)?|'
    r'club\s*mix|original\s*club\s*mix|instrumental|'
    r'original\s*video.*?'          # catches "Original Video - HD Remastered" etc.
    r')[^\)\]]*[\)\]]'
    r'|\s+[-–]\s*(hd|hq|4k|remastered)\s*$'   # trailing bare suffixes: "- HQ", "- HD Remastered"
    r'|\s+\b(hd|hq)\b\s*$',                    # trailing bare HQ/HD at end of string
    _re.IGNORECASE
)
# feat. / ft. / featuring strippers (kept in title for scoring but used cleaned for query)
_FEAT_NOISE = _re.compile(
    r'\s*[\(\[]?(feat\.?|ft\.?|featuring)\s+[^\)\]]*[\)\]]?',
    _re.IGNORECASE
)
# Multi-artist separator — for search just use the first artist
_MULTI_ARTIST = _re.compile(r'\s*[,&]\s+|\s+&\s+')


def _clean_for_search(artist: str, title: str) -> tuple[str, str]:
    """Strip noise from artist/title to improve Navidrome search hit rate."""
    # Use only the primary artist (strips feat. and multi-artist separators)
    primary_artist = _primary_artist(artist) if artist else ""
    clean_title    = _TITLE_NOISE.sub("", title).strip()
    clean_title    = _FEAT_NOISE.sub("", clean_title).strip()
    return primary_artist, clean_title


def search_song(artist: str, title: str, conf: dict) -> str | None:
    """
    Search Navidrome for a track by artist+title.
    Tries the full strings first; if nothing scores above threshold,
    retries with cleaned (noise-stripped) versions.
    Never uses a stored navidrome_id — always searches fresh.
    """
    def _run_search(q_artist: str, q_title: str) -> tuple[str | None, int, str]:
        """Returns (best_id, best_score, best_match_label)."""
        try:
            query = f"{q_artist} {q_title}".strip()
            data  = nd_get("search3", conf, {"query": query,
                                              "songCount": 15, "albumCount": 0, "artistCount": 0})
            songs = data.get("searchResult3", {}).get("song", [])
            if not songs:
                return None, 0, ""

            best_id, best_score, best_match = None, 0, ""
            for song in songs:
                song_title  = song.get("title",  "")
                song_artist = song.get("artist", "")
                title_score  = fuzzy_score(q_title,  song_title)
                artist_score = fuzzy_score(q_artist, song_artist) if q_artist else 100
                combined     = int(title_score * 0.65 + artist_score * 0.35)
                if combined > best_score:
                    best_score = combined
                    best_id    = song["id"]
                    best_match = f"{song_artist} — {song_title}"
            return best_id, best_score, best_match
        except Exception as e:
            emit_warn(f"Navidrome search error: {e}")
            return None, 0, ""

    # Pass 1: original strings
    best_id, best_score, best_match = _run_search(artist, title)
    if best_score >= FUZZY_SONG_THRESHOLD:
        emit_info(f"  ✓ Matched ({best_score}%): {best_match}")
        return best_id

    # Pass 2: cleaned strings (strip feat., HD, Radio Edit, multi-artist, etc.)
    clean_artist, clean_title = _clean_for_search(artist, title)
    if (clean_artist, clean_title) != (artist, title):
        best_id2, best_score2, best_match2 = _run_search(clean_artist, clean_title)
        if best_score2 > best_score:
            best_id, best_score, best_match = best_id2, best_score2, best_match2

    if best_score >= FUZZY_SONG_THRESHOLD:
        emit_info(f"  ✓ Matched ({best_score}%, cleaned): {best_match}")
        return best_id

    emit_warn(f"  ✗ No match for '{artist} — {title}' (best: {best_score}% '{best_match}')")
    return None


def search_song_by_path(path: str, conf: dict) -> str | None:
    parts    = Path(path).parts
    filename = Path(path).stem
    title    = filename.split(" - ", 1)[1].strip() if " - " in filename else filename.strip()
    artist   = parts[0].strip() if parts else ""
    return search_song(artist, title, conf)


def get_existing_playlist(name: str, conf: dict) -> dict | None:
    try:
        data = nd_get("getPlaylists", conf)
        for p in data.get("playlists", {}).get("playlist", []):
            if p.get("name", "").lower() == name.lower():
                return p
    except Exception as e:
        emit_warn(f"Could not fetch playlists: {e}")
    return None


def get_playlist_track_ids(playlist_id: str, conf: dict) -> list[str]:
    try:
        data   = nd_get("getPlaylist", conf, {"id": playlist_id})
        tracks = data.get("playlist", {}).get("entry", [])
        return [t["id"] for t in tracks if "id" in t]
    except Exception as e:
        emit_warn(f"Could not fetch playlist tracks: {e}")
        return []


def create_or_update_playlist(name: str, song_ids: list[str], conf: dict) -> tuple[str, str]:
    existing = get_existing_playlist(name, conf)
    if existing:
        existing_id       = existing.get("id")
        current_track_ids = get_playlist_track_ids(existing_id, conf)
        if set(current_track_ids) == set(song_ids):
            emit_info(f"Playlist '{name}' already up to date — skipping")
            return existing_id, "unchanged"
        added   = len(set(song_ids) - set(current_track_ids))
        removed = len(set(current_track_ids) - set(song_ids))
        if added:   emit_info(f"  + {added} track(s) to add")
        if removed: emit_info(f"  - {removed} track(s) to remove")
        base_params = {**make_token_auth(conf), "playlistId": existing_id}
        action      = "updated"
    else:
        base_params = {**make_token_auth(conf), "name": name}
        action      = "created"

    qs_parts = list(base_params.items()) + [("songId", sid) for sid in song_ids]
    full_url = f"{conf['navidrome']['url']}/rest/createPlaylist?{urlencode(qs_parts)}"
    resp     = requests.get(full_url, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("subsonic-response", {})
    if data.get("status") != "ok":
        raise RuntimeError(f"createPlaylist error: {data.get('error', data)}")
    pid = data.get("playlist", {}).get("id", existing.get("id") if existing else "unknown")
    return pid, action


# ═════════════════════════════════════════════════════════════════
#  M3U8 PARSER
# ═════════════════════════════════════════════════════════════════

def parse_m3u8(filepath: str) -> list[dict]:
    tracks, meta = [], {}
    with open(filepath, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line == "#EXTM3U":
                continue
            if line.startswith("#EXTINF:"):
                rest         = line[len("#EXTINF:"):]
                dur_str, lbl = rest.split(",", 1) if "," in rest else (rest, "")
                try:    dur = int(dur_str.strip())
                except: dur = -1
                lbl    = lbl.strip()
                artist = ""
                title  = lbl
                if " - " in lbl:
                    artist, title = lbl.split(" - ", 1)
                    artist = artist.strip(); title = title.strip()
                elif " • " in lbl:
                    parts  = lbl.split(" • ", 1)
                    title  = parts[0].strip()
                    artist = parts[1].strip() if len(parts) > 1 else ""
                meta = {"duration": dur, "label": lbl, "artist": artist, "title": title}
            elif not line.startswith("#"):
                path_parts  = Path(line).parts
                path_artist = path_parts[0].strip() if len(path_parts) >= 1 else ""
                path_album  = path_parts[1].strip() if len(path_parts) >= 3 else ""
                artist      = meta.get("artist", "").strip() or path_artist
                album       = path_album
                title       = meta.get("title", "").strip()
                if not title:
                    stem  = Path(line).stem
                    title = stem.split(" - ", 1)[1].strip() if " - " in stem else stem.strip()
                tracks.append({**meta, "path": line, "artist": artist, "album": album, "title": title})
                meta = {}
    return tracks


# ═════════════════════════════════════════════════════════════════
#  LIDARR API  (verbose)
# ═════════════════════════════════════════════════════════════════

def lidarr_headers(conf: dict) -> dict:
    return {"X-Api-Key": conf["lidarr"]["api_key"], "Content-Type": "application/json"}


def _lidarr_log_error(resp: requests.Response, context: str):
    """Log full Lidarr error response for debugging."""
    emit_error(f"Lidarr {context} → HTTP {resp.status_code}")
    try:
        body = resp.json()
        if isinstance(body, list):
            for err in body[:3]:
                emit_error(f"  {err.get('errorCode','?')}: {err.get('errorMessage', err)}")
        elif isinstance(body, dict):
            emit_error(f"  {body.get('message', str(body)[:200])}")
        else:
            emit_error(f"  Raw: {resp.text[:300]}")
    except Exception:
        emit_error(f"  Raw: {resp.text[:300]}")


def lidarr_get(endpoint: str, conf: dict, params: dict = None):
    url  = f"{conf['lidarr']['url']}/api/v1/{endpoint}"
    emit_debug(f"Lidarr GET {endpoint} params={params}")
    resp = requests.get(url, headers=lidarr_headers(conf), params=params, timeout=60)
    emit_debug(f"Lidarr GET {endpoint} → HTTP {resp.status_code}")
    if not resp.ok:
        _lidarr_log_error(resp, f"GET {endpoint}")
    resp.raise_for_status()
    return resp.json()


def lidarr_post(endpoint: str, conf: dict, payload: dict):
    url  = f"{conf['lidarr']['url']}/api/v1/{endpoint}"
    emit_debug(f"Lidarr POST {endpoint} payload keys={list(payload.keys())}")
    resp = requests.post(url, headers=lidarr_headers(conf), json=payload, timeout=60)
    emit_debug(f"Lidarr POST {endpoint} → HTTP {resp.status_code}")
    if not resp.ok:
        _lidarr_log_error(resp, f"POST {endpoint}")
    resp.raise_for_status()
    return resp.json()


def lidarr_find_artist(mb_id: str, conf: dict) -> dict | None:
    emit_debug(f"Lidarr: checking if artist MBID={mb_id} already exists")
    try:
        artists = lidarr_get("artist", conf)
        emit_debug(f"Lidarr: {len(artists)} artist(s) in library")
        for a in artists:
            if a.get("foreignArtistId") == mb_id:
                emit_debug(f"Lidarr: found existing artist '{a.get('artistName')}' ID={a.get('id')}")
                return a
        emit_debug(f"Lidarr: artist MBID={mb_id} not in library")
        return None
    except Exception as e:
        emit_warn(f"Lidarr artist lookup error: {e}")
        return None


def lidarr_add_artist(mb_result: dict, conf: dict) -> dict | None:
    artist_name  = mb_result["artist_name"]
    artist_mb_id = mb_result["artist_mb_id"]
    emit_info(f"  Lidarr: adding artist '{artist_name}' (MBID={artist_mb_id})")

    try:
        # Try lookup by MBID first, fall back to name
        emit_debug(f"  Lidarr: artist/lookup by lidarr:{artist_mb_id}")
        results = lidarr_get("artist/lookup", conf, {"term": f"lidarr:{artist_mb_id}"})
        emit_debug(f"  Lidarr: artist/lookup returned {len(results)} result(s)")

        if not results:
            emit_debug(f"  Lidarr: no MBID result, falling back to name search '{artist_name}'")
            results = lidarr_get("artist/lookup", conf, {"term": artist_name})
            emit_debug(f"  Lidarr: name search returned {len(results)} result(s)")

        if not results:
            emit_warn(f"  Lidarr: artist '{artist_name}' not found in any lookup")
            return None

        d = results[0]
        emit_debug(f"  Lidarr: using lookup result '{d.get('artistName')}' foreignArtistId={d.get('foreignArtistId')}")

        payload = {
            "artistName":        d.get("artistName", artist_name),
            "foreignArtistId":   d.get("foreignArtistId", artist_mb_id),
            "monitored":         True,
            "monitorNewItems":   "none",
            "qualityProfileId":  conf["lidarr"]["quality_id"],
            "metadataProfileId": conf["lidarr"]["meta_id"],
            "rootFolderPath":    conf["lidarr"]["music_path"],
            "addOptions":        {"monitor": "none", "searchForMissingAlbums": False},
            "images":            d.get("images",  []),
            "links":             d.get("links",   []),
            "genres":            d.get("genres",  []),
        }
        emit_debug(f"  Lidarr: POST artist payload — name={payload['artistName']} "
                   f"foreignId={payload['foreignArtistId']} root={payload['rootFolderPath']} "
                   f"quality={payload['qualityProfileId']} meta={payload['metadataProfileId']}")

        artist = lidarr_post("artist", conf, payload)
        emit_ok(f"  Lidarr: artist added — '{artist.get('artistName')}' ID={artist.get('id')}")
        emit_debug(f"  Waiting {conf['cooldowns']['between_artists']}s after artist add...")
        _interruptible_sleep(conf["cooldowns"]["between_artists"])
        return artist

    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 400:
            emit_warn(f"  Lidarr: 400 on artist add — artist may already exist, re-fetching")
            return lidarr_find_artist(artist_mb_id, conf)
        emit_error(f"  Lidarr: failed to add artist '{artist_name}': {e}")
        return None
    except Exception as e:
        emit_error(f"  Lidarr: unexpected error adding artist: {e}")
        return None


def lidarr_find_album(mb_album_id: str, conf: dict) -> dict | None:
    emit_debug(f"Lidarr: checking if album MBID={mb_album_id} already exists")
    try:
        results = lidarr_get("album", conf, {"foreignAlbumId": mb_album_id})
        if results:
            emit_debug(f"Lidarr: found existing album '{results[0].get('title')}' ID={results[0].get('id')}")
            return results[0]
        emit_debug(f"Lidarr: album MBID={mb_album_id} not in library")
        return None
    except Exception as e:
        emit_warn(f"Lidarr album lookup error: {e}")
        return None


def lidarr_trigger_interactive_search(album_id: int, conf: dict,
                                       expected_artist: str = "",
                                       expected_album: str = "") -> list[dict]:
    """
    Trigger Lidarr's interactive search for an album and return validated releases.
    Only returns releases whose title fuzzy-matches the expected artist + album.
    Polls until the command completes (up to 120s), then fetches and filters releases.
    """
    emit_info(f"  Lidarr: triggering interactive search for album ID={album_id}")
    try:
        cmd = lidarr_post("command", conf, {
            "name": "AlbumSearch",
            "albumIds": [album_id]
        })
        cmd_id = cmd.get("id")
        emit_debug(f"  Lidarr: command ID={cmd_id}, polling for completion...")

        # Poll until done (max 120s)
        state = "queued"
        for _ in range(40):
            _interruptible_sleep(3)
            if _should_stop():
                return []
            status = lidarr_get(f"command/{cmd_id}", conf)
            state  = status.get("status", "")
            emit_debug(f"  Lidarr: command status={state}")
            if state in ("completed", "failed"):
                break

        if state == "failed":
            emit_warn(f"  Lidarr: interactive search command failed")
            return []

        # Fetch available releases
        releases = lidarr_get("release", conf, {"albumId": album_id})
        emit_debug(f"  Lidarr: {len(releases)} release(s) returned from indexers")

        if not releases:
            return []

        # Filter to releases that actually match the expected artist + album
        # This prevents grabbing unrelated content that indexers return
        if expected_artist or expected_album:
            releases = _filter_matching_releases(releases, expected_artist, expected_album)
            emit_debug(f"  Lidarr: {len(releases)} release(s) after title validation")

        return releases

    except Exception as e:
        emit_error(f"  Lidarr: interactive search error: {e}")
        return []


def _filter_matching_releases(releases: list[dict],
                               expected_artist: str,
                               expected_album: str,
                               threshold: int = 55) -> list[dict]:
    """
    Return only releases whose title plausibly matches the expected artist + album.
    Uses fuzzy matching against the release title field.
    threshold=55 is intentionally lenient to handle remix names, deluxe editions etc.
    """
    if not expected_artist and not expected_album:
        return releases

    needle = f"{expected_artist} {expected_album}".strip().lower()
    needle_album = expected_album.lower()
    needle_artist = expected_artist.lower()

    matched = []
    for r in releases:
        title = (r.get("title") or "").lower()
        if not title:
            matched.append(r)  # no title to judge — keep it
            continue

        # Score against full "artist album" string
        score_full   = fuzz.token_set_ratio(needle, title)
        # Score just the album name portion (handles "Artist - Album" style titles)
        score_album  = fuzz.partial_ratio(needle_album, title)
        # Score just the artist name
        score_artist = fuzz.partial_ratio(needle_artist, title)

        best = max(score_full, score_album, score_artist)
        emit_debug(f"    release '{r.get('title','?')}' — match score={best} "
                   f"(full={score_full} album={score_album} artist={score_artist})")

        if best >= threshold:
            matched.append(r)
        else:
            emit_debug(f"    Discarding mismatched release: '{r.get('title','?')}' (score={best} < {threshold})")

    return matched


def lidarr_pick_best_release(releases: list[dict]) -> dict | None:
    """
    Pick the best release from an interactive search result.
    Preference order: FLAC > MP3 320 > MP3 (any), then by seeders.
    Releases should already be filtered by _filter_matching_releases.
    """
    if not releases:
        return None

    def quality_rank(r: dict) -> tuple:
        qual  = (r.get("quality", {}).get("quality", {}).get("name") or "").lower()
        title = (r.get("title") or "").lower()
        seeds = r.get("seeders", 0) or 0
        if "flac" in qual or "flac" in title:
            return (3, seeds)
        if "320" in qual or "320" in title:
            return (2, seeds)
        if "mp3" in qual or "mp3" in title:
            return (1, seeds)
        return (0, seeds)

    ranked = sorted(releases, key=quality_rank, reverse=True)
    best   = ranked[0]
    emit_info(f"  Lidarr: best release = '{best.get('title','?')}' "
              f"quality={best.get('quality',{}).get('quality',{}).get('name','?')} "
              f"seeders={best.get('seeders','?')}")
    return best


def lidarr_grab_release(release: dict, conf: dict):
    """Tell Lidarr to grab (download) a specific release."""
    emit_info(f"  Lidarr: grabbing release '{release.get('title','?')}'")
    try:
        lidarr_post("release", conf, release)
        emit_ok(f"  Lidarr: download queued ✓")
    except Exception as e:
        emit_error(f"  Lidarr: grab failed: {e}")


def lidarr_add_album(artist: dict, mb_result: dict, conf: dict,
                     track_mode: bool = False, tracks: list = None):
    album_name   = mb_result["album_name"]
    album_mb_id  = mb_result["album_mb_id"]
    artist_name  = mb_result["artist_name"]
    artist_id    = artist.get("id")
    mode_label   = "track" if track_mode else "album"
    emit_info(f"  Lidarr: adding album '{album_name}' by '{artist_name}' (MBID={album_mb_id}) [{mode_label} mode]")

    try:
        emit_debug(f"  Lidarr: album/lookup by lidarr:{album_mb_id}")
        results = lidarr_get("album/lookup", conf, {"term": f"lidarr:{album_mb_id}"})
        emit_debug(f"  Lidarr: album/lookup returned {len(results)} result(s)")

        if not results:
            term = f"{artist_name} {album_name}"
            emit_debug(f"  Lidarr: falling back to term search '{term}'")
            results = lidarr_get("album/lookup", conf, {"term": term})
            emit_debug(f"  Lidarr: term search returned {len(results)} result(s)")

        if not results:
            emit_warn(f"  Lidarr: album '{album_name}' not found in any lookup")
            return

        d = results[0]
        emit_debug(f"  Lidarr: using lookup result '{d.get('title')}' foreignAlbumId={d.get('foreignAlbumId')}")

        payload = {
            "title":            d.get("title", album_name),
            "foreignAlbumId":   d.get("foreignAlbumId", album_mb_id),
            "artistId":         artist_id,
            "monitored":        True,
            "qualityProfileId": conf["lidarr"]["quality_id"],
            "addOptions":       {"searchForNewAlbum": False},   # we do our own interactive search
            "artist":           d.get("artist",  {}),
            "images":           d.get("images",  []),
            "releases":         d.get("releases", []),
        }
        album = lidarr_post("album", conf, payload)
        album_id = album.get("id")
        emit_ok(f"  Lidarr: album added — '{album.get('title')}' ID={album_id}")
        emit_debug(f"  Waiting {conf['cooldowns']['between_albums']}s after album add...")
        _interruptible_sleep(conf["cooldowns"]["between_albums"])

        if _should_stop():
            return

        search_cooldown = conf["cooldowns"].get("interactive_search", 60)

        if track_mode and tracks:
            # Track mode: use _lidarr_action which does TrackSearch for specific tracks
            _lidarr_action(album_id, tracks or [], "track", conf,
                           expected_artist=artist_name, expected_album=album_name)
        else:
            # Album mode: interactive search for best full-album release
            emit_info(f"  Lidarr: waiting {search_cooldown}s before interactive search (rate limit)...")
            _interruptible_sleep(search_cooldown)
            if _should_stop():
                return
            releases = lidarr_trigger_interactive_search(
                album_id, conf,
                expected_artist=artist_name,
                expected_album=album_name,
            )
            if releases:
                best = lidarr_pick_best_release(releases)
                if best:
                    lidarr_grab_release(best, conf)
                else:
                    emit_warn("  Lidarr: no suitable release found — Lidarr will search on its own schedule")
            else:
                emit_warn("  Lidarr: interactive search returned no releases — Lidarr will search on its own schedule")

    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 400:
            emit_warn(f"  Lidarr: 400 on album add — album may already exist in Lidarr")
        else:
            emit_error(f"  Lidarr: failed to add album '{album_name}': {e}\n"
                       f"  Response: {e.response.text if e.response else 'no response'}")
    except Exception as e:
        import traceback
        emit_error(f"  Lidarr: unexpected error adding album '{album_name}': {e}\n"
                   f"  {traceback.format_exc()}")


# ═════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ═════════════════════════════════════════════════════════════════

COMPILATION_ARTISTS = {
    "various artists", "various", "v.a.", "va",
    "soundtrack", "ost", "original soundtrack",
}


def _lidarr_action(album_id: int, tracks: list[dict], lidarr_mode: str, conf: dict,
                   expected_artist: str = "", expected_album: str = ""):
    """
    Trigger the appropriate Lidarr action for an album that already exists
    but isn't fully downloaded.
    """
    search_cooldown = conf["cooldowns"].get("interactive_search", 60)
    if lidarr_mode == "monitor_only":
        emit_info("  Lidarr: monitor_only mode — not triggering search")
        return

    if lidarr_mode == "track":
        # Try to find specific track IDs in Lidarr and search for just those
        emit_info(f"  Lidarr: track mode — searching for {len(tracks)} specific track(s)")
        for t in tracks:
            if _should_stop():
                return
            title  = t.get("title", "")
            artist = t.get("artist", "")
            try:
                track_results = lidarr_get("track", conf, {"albumId": album_id})
                matched = None
                for lt in track_results:
                    lt_title = lt.get("title", "").lower()
                    if lt_title and lt_title in title.lower() or title.lower() in lt_title:
                        matched = lt
                        break
                if matched:
                    track_id = matched.get("id")
                    emit_info(f"  Lidarr: found track ID={track_id} for '{title}' — triggering TrackSearch")
                    _interruptible_sleep(search_cooldown)
                    if not _should_stop():
                        lidarr_post("command", conf, {"name": "TrackSearch", "trackIds": [track_id]})
                        emit_ok(f"  Lidarr: TrackSearch queued for '{title}'")
                else:
                    emit_warn(f"  Lidarr: could not find track '{title}' in album — falling back to AlbumSearch")
                    _interruptible_sleep(search_cooldown)
                    if not _should_stop():
                        releases = lidarr_trigger_interactive_search(
                            album_id, conf,
                            expected_artist=expected_artist or artist,
                            expected_album=expected_album,
                        )
                        if releases:
                            best = lidarr_pick_best_release(releases)
                            if best:
                                lidarr_grab_release(best, conf)
            except Exception as e:
                emit_warn(f"  Lidarr: track search error for '{title}': {e}")
    else:
        # album mode — grab the whole album
        emit_info(f"  Lidarr: album mode — triggering AlbumSearch")
        _interruptible_sleep(search_cooldown)
        if not _should_stop():
            releases = lidarr_trigger_interactive_search(
                album_id, conf,
                expected_artist=expected_artist,
                expected_album=expected_album,
            )
            if releases:
                best = lidarr_pick_best_release(releases)
                if best:
                    lidarr_grab_release(best, conf)
                else:
                    emit_warn("  Lidarr: no suitable release found")
            else:
                emit_warn("  Lidarr: interactive search returned no results")


def lidarr_add_album_monitor_only(artist: dict, mb_result: dict, conf: dict):
    """Add an album to Lidarr with monitoring enabled but no immediate search."""
    album_name  = mb_result["album_name"]
    album_mb_id = mb_result["album_mb_id"]
    artist_name = mb_result["artist_name"]
    artist_id   = artist.get("id")
    emit_info(f"  Lidarr: adding album '{album_name}' (monitor only — no search)")
    try:
        results = lidarr_get("album/lookup", conf, {"term": f"lidarr:{album_mb_id}"})
        if not results:
            results = lidarr_get("album/lookup", conf, {"term": f"{artist_name} {album_name}"})
        if not results:
            emit_warn(f"  Lidarr: album '{album_name}' not found in lookup")
            return
        d = results[0]
        payload = {
            "title":            d.get("title", album_name),
            "foreignAlbumId":   d.get("foreignAlbumId", album_mb_id),
            "artistId":         artist_id,
            "monitored":        True,
            "qualityProfileId": conf["lidarr"]["quality_id"],
            "addOptions":       {"searchForNewAlbum": False},
            "artist":           d.get("artist",  {}),
            "images":           d.get("images",  []),
            "releases":         d.get("releases", []),
        }
        album = lidarr_post("album", conf, payload)
        emit_ok(f"  Lidarr: album added (monitor only) — '{album.get('title')}' ID={album.get('id')}")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 400:
            emit_warn(f"  Lidarr: 400 on album add — may already exist")
        else:
            emit_error(f"  Lidarr: failed to add album '{album_name}': {e}")
    except Exception as e:
        emit_error(f"  Lidarr: unexpected error adding album '{album_name}': {e}")


def process_missing(missing: list[dict], conf: dict, lidarr_mode: str = "album"):
    """
    Send missing tracks to Lidarr.

    lidarr_mode controls what happens when artist+album are found in Lidarr:
      "album"        — grab the whole album (default). Best for complete library.
      "track"        — search for the specific track only via TrackSearch command.
                       Better for playlists where you only want specific songs.
      "monitor_only" — add artist/album to Lidarr and monitor but don't trigger
                       an immediate search. Lidarr will find it on its own schedule.

    Logic per track:
      1. Resolve artist via MusicBrainz.
      2. If artist NOT in Lidarr → add artist (monitored), stop here.
      3. If artist IS in Lidarr and album is known → check/add album, then act
         according to lidarr_mode.
      4. If artist IS in Lidarr and album unknown → nothing more to do for now.
    """
    if not missing:
        return

    emit_info(f"  Lidarr mode: {lidarr_mode}")

    # Deduplicate into (artist, album) buckets
    with_album    = {}
    without_album = {}

    for t in missing:
        if t.get("mb_artist_id") and t.get("mb_album_id"):
            emit_debug(f"  MB IDs cached for '{t.get('title','?')}' — skipping MB lookup")

        artist = _primary_artist(t.get("artist", "").strip())
        album  = t.get("album", "").strip()

        if t.get("path"):
            parts = Path(t["path"]).parts
            if not artist and len(parts) >= 1: artist = parts[0].strip()
            if not album  and len(parts) >= 3: album  = parts[1].strip()

        if not artist:
            emit_warn(f"  Cannot determine artist — skipping: {t.get('title','?')}")
            continue
        if artist.lower() in COMPILATION_ARTISTS:
            emit_warn(f"  Skipping compilation/VA: '{artist}'")
            continue
        if album and artist.lower() == album.lower():
            emit_warn(f"  Artist == Album ('{artist}') — parse error, ignoring album")
            album = ""
        if album:
            album_l = album.lower()
            if any(kw in album_l for kw in (
                "ski mix","body combat","party party","hits des jahres",
                "hits of the year","best of 20","ministry of sound",
                "annual 20","compilation","various","v.a.",
                "remastered","original video",
            )):
                emit_debug(f"  Album '{album}' looks like a compilation/mix — ignoring for Lidarr")
                album = ""

        if album:
            key = (artist.lower(), album.lower())
            if key not in with_album:
                with_album[key] = {"artist": artist, "album": album, "tracks": []}
            with_album[key]["tracks"].append(t)
        else:
            key = artist.lower()
            if key not in without_album:
                without_album[key] = {"artist": artist, "tracks": []}
            without_album[key]["tracks"].append(t)

    total = len(with_album) + len(without_album)
    if total == 0:
        emit_warn("No valid tracks to send to Lidarr")
        return

    emit_info(f"Lidarr queue: {len(with_album)} with album, {len(without_album)} artist-only")
    conf = cfg.load()
    idx  = 0

    # ── Tracks with known album ───────────────────────────────────
    for entry in with_album.values():
        if _should_stop():
            emit_warn("⏹ Stop requested — aborting Lidarr phase")
            return
        artist_name, album_name = entry["artist"], entry["album"]
        idx += 1
        emit_info(f"─── [{idx}/{total}] {artist_name} — {album_name}")

        mb_result = mb_lookup_release(artist_name, album_name, conf)
        if not mb_result:
            emit_warn(f"  MusicBrainz: no match for '{artist_name} / {album_name}' — skipping")
            continue

        emit_debug(f"  MB: artist='{mb_result['artist_name']}' MBID={mb_result['artist_mb_id']} "
                   f"album='{mb_result['album_name']}' MBID={mb_result['album_mb_id']}")
        for t in entry["tracks"]:
            t["mb_artist_id"] = mb_result["artist_mb_id"]
            t["mb_album_id"]  = mb_result["album_mb_id"]

        artist_obj = lidarr_find_artist(mb_result["artist_mb_id"], conf)
        if not artist_obj:
            emit_info(f"  Artist not in Lidarr — adding now. Album '{album_name}' "
                      f"will be queued on the next sync pass.")
            lidarr_add_artist(mb_result, conf)
        else:
            emit_info(f"  Lidarr: artist already present (ID={artist_obj.get('id')})")
            existing_album = lidarr_find_album(mb_result["album_mb_id"], conf)
            if existing_album:
                stats    = existing_album.get("statistics", {})
                total_t  = stats.get("totalTrackCount", 0)
                on_disk  = stats.get("trackFileCount",  0)
                album_id = existing_album.get("id")
                if on_disk >= total_t and total_t > 0:
                    emit_info(f"  Lidarr: album fully downloaded ({on_disk}/{total_t} tracks) "
                              f"— Navidrome may not have scanned yet")
                else:
                    emit_warn(f"  Lidarr: album exists but only {on_disk}/{total_t} tracks on disk "
                              f"— mode={lidarr_mode}")
                    _lidarr_action(album_id, entry["tracks"], lidarr_mode, conf,
                                   expected_artist=artist_name, expected_album=album_name)
            else:
                if lidarr_mode == "monitor_only":
                    # Add album but don't search
                    lidarr_add_album_monitor_only(artist_obj, mb_result, conf)
                else:
                    # album and track mode both add the album and search
                    # (track mode does TrackSearch after album is added)
                    lidarr_add_album(artist_obj, mb_result, conf,
                                     track_mode=(lidarr_mode == "track"),
                                     tracks=entry["tracks"])

        if idx < total:
            wait = conf["cooldowns"]["between_searches"]
            emit_info(f"  Cooldown {wait}s before next entry...")
            _interruptible_sleep(wait)

    # ── Tracks with unknown album — ensure artist is monitored ────
    for entry in without_album.values():
        if _should_stop():
            emit_warn("⏹ Stop requested — aborting Lidarr phase")
            return
        artist_name = entry["artist"]
        idx += 1
        emit_info(f"─── [{idx}/{total}] {artist_name} (no album — ensuring artist is monitored)")

        mb_artist = mb_search_artist(artist_name, conf)
        if not mb_artist:
            emit_warn(f"  MusicBrainz: artist '{artist_name}' not found — skipping")
            continue

        mb_id = mb_artist.get("id", "")
        for t in entry["tracks"]:
            t["mb_artist_id"] = mb_id

        artist_obj = lidarr_find_artist(mb_id, conf)
        if artist_obj:
            emit_info(f"  Lidarr: artist already in library (ID={artist_obj.get('id')}) — nothing to add")
        else:
            emit_info(f"  Lidarr: adding artist with monitor=none")
            try:
                results = lidarr_get("artist/lookup", conf, {"term": f"lidarr:{mb_id}"})
                if not results:
                    results = lidarr_get("artist/lookup", conf, {"term": artist_name})
                if not results:
                    emit_warn(f"  Lidarr: artist '{artist_name}' not found in lookup")
                    continue
                d = results[0]
                payload = {
                    "artistName":        d.get("artistName", artist_name),
                    "foreignArtistId":   d.get("foreignArtistId", mb_id),
                    "monitored":         True,
                    "monitorNewItems":   "none",
                    "qualityProfileId":  conf["lidarr"]["quality_id"],
                    "metadataProfileId": conf["lidarr"]["meta_id"],
                    "rootFolderPath":    conf["lidarr"]["music_path"],
                    "addOptions":        {"monitor": "none", "searchForMissingAlbums": False},
                    "images":  d.get("images",  []),
                    "links":   d.get("links",   []),
                    "genres":  d.get("genres",  []),
                }
                artist_obj = lidarr_post("artist", conf, payload)
                emit_ok(f"  Lidarr: artist added — '{artist_obj.get('artistName')}' "
                        f"ID={artist_obj.get('id')}")
                _interruptible_sleep(conf["cooldowns"]["between_artists"])
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 400:
                    emit_warn(f"  Lidarr: 400 — artist may already exist")
                else:
                    emit_error(f"  Lidarr: error adding artist '{artist_name}': {e}\n"
                               f"  Response: {e.response.text if e.response else 'no response'}")
            except Exception as e:
                import traceback
                emit_error(f"  Lidarr: unexpected error for '{artist_name}': {e}\n"
                           f"  {traceback.format_exc()}")

        if idx < total:
            wait = conf["cooldowns"]["between_searches"]
            emit_info(f"  Cooldown {wait}s before next entry...")
            _interruptible_sleep(wait)


def run_import(playlist_dir: str, log_path: str):
    global _is_running
    conf = cfg.load()
    _load_mb_cache()
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    log_lines     = []
    original_emit = globals()["emit"]

    def emit_and_log(level: str, msg: str):
        original_emit(level, msg)
        log_lines.append(f"[{datetime.now().strftime('%H:%M:%S')}] [{level}] {msg}\n")

    globals()["emit"] = emit_and_log

    try:
        emit_info("═" * 50)
        emit_info(f"Import started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        emit_info("═" * 50)

        try:
            nd_get("ping", conf)
            emit_ok(f"Navidrome connected at {conf['navidrome']['url']}")
        except Exception as e:
            emit_error(f"Cannot reach Navidrome: {e}")
            return

        lidarr_ok = True
        try:
            status = lidarr_get("system/status", conf)
            emit_ok(f"Lidarr connected at {conf['lidarr']['url']} — version {status.get('version','?')}")
        except Exception as e:
            emit_warn(f"Cannot reach Lidarr: {e} — missing tracks won't be forwarded")
            lidarr_ok = False

        folder    = Path(playlist_dir)
        m3u_files = sorted(folder.glob("*.m3u8")) + sorted(folder.glob("*.m3u"))
        if not m3u_files:
            emit_warn(f"No .m3u/.m3u8 files found in {playlist_dir}")
            return

        emit_info(f"Found {len(m3u_files)} playlist file(s)")
        emit_info("─" * 40)
        emit_info("PHASE 1: Navidrome playlist import")
        emit_info("─" * 40)

        all_missing = []
        for m3u_file in m3u_files:
            name   = m3u_file.stem.replace("_", " ").replace("-", " ").strip()
            emit_info(f"Processing: '{name}'")
            tracks = parse_m3u8(str(m3u_file))
            emit_info(f"{len(tracks)} track(s) parsed from file")
            song_ids, not_found = [], []
            for t in tracks:
                sid = None
                if t.get("artist") and t.get("title"):
                    sid = search_song(t["artist"], t["title"], conf)
                if not sid and t.get("path"):
                    sid = search_song_by_path(t["path"], conf)
                if sid:
                    song_ids.append(sid)
                else:
                    not_found.append(t)
            if song_ids:
                pid, action = create_or_update_playlist(name, song_ids, conf)
                if action != "unchanged":
                    emit_ok(f"Playlist '{name}' {action} — {len(song_ids)}/{len(tracks)} tracks")
                else:
                    emit_ok(f"Playlist '{name}' unchanged — {len(song_ids)} tracks")
            else:
                emit_error(f"No tracks matched — '{name}' not created")
            all_missing.extend(not_found)

        if lidarr_ok and all_missing:
            emit_info("─" * 40)
            emit_info("PHASE 2: MusicBrainz → Lidarr")
            emit_info("─" * 40)
            process_missing(all_missing, conf)
        elif not all_missing:
            emit_ok("All tracks found in Navidrome — nothing to send to Lidarr")

        emit_info("═" * 50)
        emit_ok(f"Import complete at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        emit_info("═" * 50)

    except Exception as e:
        emit_error(f"Import failed: {e}")
    finally:
        globals()["emit"] = original_emit
        _is_running = False
        progress_queue.put({"level": "DONE", "msg": "Import finished",
                            "ts": datetime.now().strftime("%H:%M:%S")})
        with open(log_path, "w", encoding="utf-8") as f:
            f.writelines(log_lines)
        _save_mb_cache()


def start_import(playlist_dir: str, log_dir: str) -> bool:
    global _is_running
    with _run_lock:
        if _is_running:
            return False
        _is_running = True
    while not progress_queue.empty():
        progress_queue.get_nowait()
    log_path = Path(log_dir) / f"import_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    threading.Thread(target=run_import, args=(playlist_dir, str(log_path)), daemon=True).start()
    return True


def is_running() -> bool:
    return _is_running
