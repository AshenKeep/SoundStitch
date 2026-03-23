"""
YouTube Music integration.
Supports two methods:
  1. OAuth via YouTube Data API v3
  2. ytmusicapi with browser headers (cookie-based, no OAuth needed)
"""

import time
import threading
import requests
from rapidfuzz import fuzz
from app import config as cfg

YT_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
YT_TOKEN_URL = "https://oauth2.googleapis.com/token"
YT_API_URL   = "https://www.googleapis.com/youtube/v3"
YT_SCOPES    = "https://www.googleapis.com/auth/youtube"

_oauth_state: dict = {}

# Cache the YTMusic instance so we don't re-parse headers on every call
_ytmusic_instance = None
_ytmusic_lock     = threading.Lock()
_ytmusic_headers  = ""    # fingerprint — invalidate cache if headers change

# How similar a YT search result must be to what we asked for (0–100)
YT_SEARCH_THRESHOLD = 60


# ── OAuth method ─────────────────────────────────────────────────

def get_auth_url() -> str:
    import secrets
    conf  = cfg.load()
    state = secrets.token_hex(16)
    _oauth_state["state"] = state

    params = {
        "client_id":     conf["youtube"]["client_id"],
        "redirect_uri":  f"https://localhost:8443/api/youtube/callback",
        "response_type": "code",
        "scope":         YT_SCOPES,
        "access_type":   "offline",
        "prompt":        "consent",
        "state":         state,
    }
    return YT_AUTH_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())


def handle_callback(code: str, state: str) -> bool:
    if state != _oauth_state.get("state"):
        return False

    conf = cfg.load()
    resp = requests.post(YT_TOKEN_URL, data={
        "code":          code,
        "client_id":     conf["youtube"]["client_id"],
        "client_secret": conf["youtube"]["client_secret"],
        "redirect_uri":  "https://localhost:8443/api/youtube/callback",
        "grant_type":    "authorization_code",
    }, timeout=20)
    if not resp.ok:
        return False

    data = resp.json()
    conf["youtube"]["access_token"]  = data.get("access_token", "")
    conf["youtube"]["refresh_token"] = data.get("refresh_token", conf["youtube"]["refresh_token"])
    conf["youtube"]["token_expiry"]  = time.time() + data.get("expires_in", 3600) - 60
    conf["youtube"]["enabled"]       = True
    cfg.save(conf)
    return True


def _refresh_oauth(conf: dict) -> dict:
    resp = requests.post(YT_TOKEN_URL, data={
        "grant_type":    "refresh_token",
        "refresh_token": conf["youtube"]["refresh_token"],
        "client_id":     conf["youtube"]["client_id"],
        "client_secret": conf["youtube"]["client_secret"],
    }, timeout=20)
    if resp.ok:
        data = resp.json()
        conf["youtube"]["access_token"] = data.get("access_token", "")
        conf["youtube"]["token_expiry"] = time.time() + data.get("expires_in", 3600) - 60
        cfg.save(conf)
    return conf


def _oauth_headers() -> dict:
    conf = cfg.load()
    if time.time() >= conf["youtube"]["token_expiry"]:
        conf = _refresh_oauth(conf)
    return {"Authorization": f"Bearer {conf['youtube']['access_token']}"}


# ── ytmusicapi (headers) method ───────────────────────────────────

def _headers_raw_to_ytmusic_auth(raw: str) -> str:
    """
    Convert raw browser headers (copy-pasted from Firefox/Chrome DevTools)
    into the JSON dict that ytmusicapi's YTMusic(auth=...) constructor needs.

    ytmusicapi needs these keys extracted from the browser headers:
      - Cookie
      - User-Agent
      - Authorization  (SAPISIDHASH ... — may be absent on some requests)
      - X-Goog-AuthUser
      - X-Origin / Origin

    If the input is already valid JSON (previously converted), return as-is.
    """
    import json as _json
    import re as _re

    stripped = raw.strip()

    # Already in JSON format?
    if stripped.startswith("{"):
        try:
            _json.loads(stripped)
            return stripped
        except Exception:
            pass

    # Parse the raw header block into a key→value dict.
    # Handles both Firefox "Raw" view (includes the request line) and
    # plain "key: value" dumps from Chrome/curl.
    headers: dict[str, str] = {}
    for line in stripped.splitlines():
        line = line.strip()
        if not line or line.startswith("POST ") or line.startswith("GET ") or line.startswith("HTTP/"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key and val:
            headers[key] = val

    # Must have at least a cookie to be useful
    if "cookie" not in headers:
        raise RuntimeError(
            "No 'Cookie' header found in the pasted text. "
            "Make sure you copied the full Request Headers block from the Network tab "
            "(use the Raw toggle in Firefox, or right-click → Copy → Copy request headers in Chrome)."
        )

    # Build the auth dict ytmusicapi expects
    auth = {
        "User-Agent":      headers.get("user-agent", "Mozilla/5.0"),
        "Accept":          "*/*",
        "Accept-Language": headers.get("accept-language", "en-US,en;q=0.5"),
        "Content-Type":    "application/json",
        "X-Goog-AuthUser": headers.get("x-goog-authuser", "0"),
        "x-origin":        headers.get("x-origin") or headers.get("origin", "https://music.youtube.com"),
        "Cookie":          headers["cookie"],
    }

    # Authorization (SAPISIDHASH) — present on most browse requests
    if "authorization" in headers:
        auth["Authorization"] = headers["authorization"]

    return _json.dumps(auth)


def _get_ytmusic():
    """
    Return a cached YTMusic instance.
    Re-creates it only if the stored headers have changed.
    Thread-safe via _ytmusic_lock.
    """
    global _ytmusic_instance, _ytmusic_headers
    conf = cfg.load()
    raw  = conf["youtube"].get("headers_raw", "")
    if not raw:
        raise RuntimeError("No YouTube Music headers configured")
    with _ytmusic_lock:
        if _ytmusic_instance is None or raw != _ytmusic_headers:
            from ytmusicapi import YTMusic
            auth = _headers_raw_to_ytmusic_auth(raw)
            _ytmusic_instance = YTMusic(auth=auth)
            _ytmusic_headers  = raw
    return _ytmusic_instance


def _invalidate_ytmusic_cache():
    global _ytmusic_instance, _ytmusic_headers
    with _ytmusic_lock:
        _ytmusic_instance = None
        _ytmusic_headers  = ""


# ── Unified interface ─────────────────────────────────────────────

def is_connected(emit=None) -> bool:
    def _log(msg):
        if emit:
            emit(f"[YT] {msg}")

    conf   = cfg.load()
    yt     = conf["youtube"]
    if not yt.get("enabled"):
        _log("Not enabled — configure YouTube Music in Settings")
        return False
    method = yt.get("method", "oauth")
    if method == "headers":
        if not yt.get("headers_raw"):
            _log("Headers method selected but no headers pasted — please add headers in Settings")
            return False
        return True
    # OAuth
    if not yt.get("access_token"):
        _log("No access token — YouTube Music needs to be reconnected via Settings")
        return False
    return True


def get_playlists() -> list[dict]:
    conf   = cfg.load()
    method = conf["youtube"].get("method", "oauth")

    if method == "headers":
        yt    = _get_ytmusic()
        items = yt.get_library_playlists(limit=100)
        return [{"id": p["playlistId"], "name": p["title"], "tracks": p.get("count", 0)}
                for p in items]
    else:
        # OAuth — YouTube Data API v3
        playlists, token = [], None
        while True:
            params = {"part": "snippet,contentDetails", "mine": "true", "maxResults": 50}
            if token:
                params["pageToken"] = token
            resp = requests.get(f"{YT_API_URL}/playlists", headers=_oauth_headers(),
                                 params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            for p in data.get("items", []):
                playlists.append({
                    "id":     p["id"],
                    "name":   p["snippet"]["title"],
                    "tracks": p["contentDetails"]["itemCount"],
                })
            token = data.get("nextPageToken")
            if not token:
                break
        return playlists


def get_playlist_tracks(playlist_id: str) -> list[dict]:
    conf   = cfg.load()
    method = conf["youtube"].get("method", "oauth")

    if method == "headers":
        yt     = _get_ytmusic()
        items  = yt.get_playlist(playlist_id, limit=None).get("tracks", [])
        tracks = []
        for t in items:
            artists = ", ".join(a["name"] for a in t.get("artists", []))
            alb_obj = t.get("album")
            album   = alb_obj.get("name", "") if isinstance(alb_obj, dict) else ""
            tracks.append({
                "artist":     artists,
                "title":      t.get("title", ""),
                "album":      album,
                "youtube_id": t.get("videoId", ""),
                "source":     "youtube",
            })
        return tracks
    else:
        tracks, token = [], None
        while True:
            params = {
                "part":       "snippet",
                "playlistId": playlist_id,
                "maxResults": 50,
            }
            if token:
                params["pageToken"] = token
            resp = requests.get(f"{YT_API_URL}/playlistItems",
                                 headers=_oauth_headers(), params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("items", []):
                snip = item["snippet"]
                tracks.append({
                    "artist":     snip.get("videoOwnerChannelTitle", "").replace(" - Topic", ""),
                    "title":      snip["title"],
                    "youtube_id": snip["resourceId"]["videoId"],
                    "source":     "youtube",
                })
            token = data.get("nextPageToken")
            if not token:
                break
        return tracks


def _yt_fuzzy_score(q_artist: str, q_title: str, r_artist: str, r_title: str) -> int:
    """Score a YT result against a query. Returns 0–100."""
    title_score  = max(
        fuzz.ratio(q_title.lower(),        r_title.lower()),
        fuzz.partial_ratio(q_title.lower(), r_title.lower()),
        fuzz.token_sort_ratio(q_title.lower(), r_title.lower()),
    )
    if q_artist:
        artist_score = max(
            fuzz.ratio(q_artist.lower(),        r_artist.lower()),
            fuzz.partial_ratio(q_artist.lower(), r_artist.lower()),
            fuzz.token_sort_ratio(q_artist.lower(), r_artist.lower()),
        )
    else:
        artist_score = 100
    return int(title_score * 0.65 + artist_score * 0.35)


def _strip_noise(s: str) -> str:
    """Remove common noise words from a title for a cleaner search query."""
    import re
    s = re.sub(r'\s*[\(\[].{0,50}[\)\]]', '', s)          # bracketed suffixes
    s = re.sub(r'\s*-\s*(official|audio|video|hq|hd|remix|radio edit|original mix).*$',
               '', s, flags=re.IGNORECASE)
    s = re.sub(r'\s+feat\.?\s+.*$', '', s, flags=re.IGNORECASE)
    return s.strip()


def search_track(artist: str, title: str, emit_fn=None) -> str | None:
    """
    Search YouTube Music for a track.
    Returns videoId of the best-matching result, or None.
    Does two passes: full strings, then noise-stripped strings.
    Validates each candidate with fuzzy matching before accepting.
    """
    def _log(msg):
        if emit_fn:
            emit_fn(msg)

    conf   = cfg.load()
    method = conf["youtube"].get("method", "oauth")

    def _search_headers(q_artist: str, q_title: str):
        """Returns list of (score, videoId, result_artist, result_title)."""
        try:
            yt      = _get_ytmusic()
            query   = f"{q_artist} {q_title}".strip()
            results = yt.search(query, filter="songs", limit=10)
            scored  = []
            for r in results:
                r_title  = r.get("title", "")
                r_artist = ", ".join(a["name"] for a in r.get("artists", []))
                score    = _yt_fuzzy_score(q_artist, q_title, r_artist, r_title)
                scored.append((score, r.get("videoId"), r_artist, r_title))
            return sorted(scored, key=lambda x: x[0], reverse=True)
        except Exception as e:
            _log(f"  [YT] headers search error: {e}")
            return []

    def _search_oauth(q_artist: str, q_title: str):
        try:
            resp = requests.get(f"{YT_API_URL}/search", headers=_oauth_headers(), params={
                "part":            "snippet",
                "q":               f"{q_artist} {q_title}".strip(),
                "type":            "video",
                "videoCategoryId": "10",
                "maxResults":      10,
            }, timeout=20)
            if not resp.ok:
                return []
            scored = []
            for item in resp.json().get("items", []):
                snip     = item["snippet"]
                r_title  = snip.get("title", "")
                r_artist = snip.get("channelTitle", "").replace(" - Topic", "")
                score    = _yt_fuzzy_score(q_artist, q_title, r_artist, r_title)
                scored.append((score, item["id"]["videoId"], r_artist, r_title))
            return sorted(scored, key=lambda x: x[0], reverse=True)
        except Exception as e:
            _log(f"  [YT] oauth search error: {e}")
            return []

    def _pick(candidates, label: str):
        if not candidates:
            return None
        best_score, best_vid, best_artist, best_title = candidates[0]
        if best_score >= YT_SEARCH_THRESHOLD:
            _log(f"  [YT] ✓ Match ({best_score}%, {label}): {best_artist} — {best_title}")
            return best_vid
        _log(f"  [YT] ✗ Best candidate ({best_score}%, {label}): {best_artist} — {best_title} — below threshold")
        return None

    _search = _search_headers if method == "headers" else _search_oauth

    # Pass 1: full strings
    candidates = _search(artist, title)
    result = _pick(candidates, "full")
    if result:
        return result

    # Pass 2: noise-stripped
    clean_artist = _strip_noise(artist)
    clean_title  = _strip_noise(title)
    if (clean_artist, clean_title) != (artist, title):
        candidates2 = _search(clean_artist, clean_title)
        result = _pick(candidates2, "cleaned")
        if result:
            return result

    # Pass 3: title-only (artist name often causes mismatches)
    candidates3 = _search("", title)
    result = _pick(candidates3, "title-only")
    if result:
        return result

    _log(f"  [YT] ✗ No match found for: {artist} — {title}")
    return None


def add_tracks_to_playlist(playlist_id: str, video_ids: list[str]):
    conf   = cfg.load()
    method = conf["youtube"].get("method", "oauth")

    if method == "headers":
        yt = _get_ytmusic()
        # Add in batches of 25 to avoid single giant request
        BATCH = 25
        for i in range(0, len(video_ids), BATCH):
            batch = video_ids[i:i+BATCH]
            try:
                yt.add_playlist_items(playlist_id, batch)
                if len(video_ids) > BATCH:
                    time.sleep(2)   # brief pause between batches
            except Exception as e:
                # Fall back to one-at-a-time if batch fails
                for vid in batch:
                    try:
                        yt.add_playlist_items(playlist_id, [vid])
                    except Exception:
                        pass
    else:
        for vid in video_ids:
            requests.post(f"{YT_API_URL}/playlistItems", headers=_oauth_headers(),
                          params={"part": "snippet"},
                          json={"snippet": {
                              "playlistId": playlist_id,
                              "resourceId": {"kind": "youtube#video", "videoId": vid}
                          }}, timeout=20)


def create_playlist(name: str) -> str:
    conf   = cfg.load()
    method = conf["youtube"].get("method", "oauth")

    if method == "headers":
        yt     = _get_ytmusic()
        result = yt.create_playlist(name, "")
        return result
    else:
        resp = requests.post(f"{YT_API_URL}/playlists", headers=_oauth_headers(),
                             params={"part": "snippet"},
                             json={"snippet": {"title": name}}, timeout=20)
        resp.raise_for_status()
        return resp.json()["id"]


# ── OAuth method ─────────────────────────────────────────────────

