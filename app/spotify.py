"""
Spotify integration — OAuth PKCE flow + playlist read/write.
Tokens are stored encrypted by config.py.  Auto-refresh is attempted on
every 401 response before giving up — handles Spotify's rotating refresh tokens.
"""

import time
import secrets
import hashlib
import base64
import requests
from app import config as cfg

SPOTIFY_AUTH_URL  = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_URL   = "https://api.spotify.com/v1"
SCOPES            = ("playlist-read-private playlist-read-collaborative "
                     "playlist-modify-public playlist-modify-private")

_pkce_state: dict = {}


def _verifier_and_challenge() -> tuple[str, str]:
    verifier  = secrets.token_urlsafe(64)
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def get_auth_url() -> str:
    conf                = cfg.load()
    verifier, challenge = _verifier_and_challenge()
    state               = secrets.token_hex(16)
    _pkce_state[state]  = verifier

    params = {
        "client_id":             conf["spotify"]["client_id"],
        "response_type":         "code",
        "redirect_uri":          conf["spotify"]["redirect_uri"],
        "scope":                 SCOPES,
        "state":                 state,
        "code_challenge_method": "S256",
        "code_challenge":        challenge,
    }
    return SPOTIFY_AUTH_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())


def handle_callback(code: str, state: str) -> bool:
    verifier = _pkce_state.pop(state, None)
    if not verifier:
        return False

    conf = cfg.load()
    resp = requests.post(SPOTIFY_TOKEN_URL, data={
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  conf["spotify"]["redirect_uri"],
        "client_id":     conf["spotify"]["client_id"],
        "code_verifier": verifier,
    }, timeout=20)
    if not resp.ok:
        return False

    data = resp.json()
    conf["spotify"]["access_token"]  = data.get("access_token", "")
    # Spotify sometimes rotates refresh_token — always store the latest one
    if data.get("refresh_token"):
        conf["spotify"]["refresh_token"] = data["refresh_token"]
    conf["spotify"]["token_expiry"]  = time.time() + data.get("expires_in", 3600) - 60
    conf["spotify"]["enabled"]       = True
    cfg.save(conf)
    return True


def _refresh_token(conf: dict) -> dict:
    """
    Exchange the stored refresh_token for a fresh access_token.
    Spotify may also return a new refresh_token (rotating tokens) — we store it.
    Returns updated conf on success; on failure the access_token is cleared.
    """
    rt = conf["spotify"].get("refresh_token", "")
    if not rt:
        conf["spotify"]["access_token"] = ""
        return conf

    resp = requests.post(SPOTIFY_TOKEN_URL, data={
        "grant_type":    "refresh_token",
        "refresh_token": rt,
        "client_id":     conf["spotify"]["client_id"],
    }, timeout=20)

    if resp.ok:
        data = resp.json()
        conf["spotify"]["access_token"] = data.get("access_token", "")
        if data.get("refresh_token"):                       # rotating token
            conf["spotify"]["refresh_token"] = data["refresh_token"]
        conf["spotify"]["token_expiry"] = time.time() + data.get("expires_in", 3600) - 60
        cfg.save(conf)
    else:
        conf["spotify"]["access_token"] = ""
        cfg.save(conf)
    return conf


def _get_valid_token(emit_fn=None) -> str:
    """
    Return a valid access token, refreshing if needed.
    Raises RuntimeError if the token cannot be obtained.
    """
    def _log(msg):
        if emit_fn:
            emit_fn(f"[SP] {msg}")

    conf = cfg.load()
    sp   = conf["spotify"]

    if not sp.get("enabled"):
        raise RuntimeError("Spotify not enabled")

    if time.time() >= sp.get("token_expiry", 0) or not sp.get("access_token"):
        _log("Token expired — refreshing...")
        conf = _refresh_token(conf)
        sp   = conf["spotify"]

    token = sp.get("access_token", "")
    if not token:
        raise RuntimeError("No valid Spotify access token — please reconnect in Settings")
    return token


def _headers(emit_fn=None) -> dict:
    return {"Authorization": f"Bearer {_get_valid_token(emit_fn)}"}


def _api_get(url: str, params: dict = None, emit_fn=None) -> requests.Response:
    """GET with automatic one-retry on 401 (token refresh)."""
    resp = requests.get(url, headers=_headers(emit_fn), params=params, timeout=20)
    if resp.status_code == 401:
        # Token may have just expired mid-run — refresh and retry once
        conf = cfg.load()
        conf = _refresh_token(conf)
        cfg.save(conf)
        resp = requests.get(url, headers=_headers(emit_fn), params=params, timeout=20)
    return resp


def _api_post(url: str, json_body: dict, params: dict = None, emit_fn=None) -> requests.Response:
    """POST with automatic one-retry on 401."""
    resp = requests.post(url, headers=_headers(emit_fn),
                         params=params, json=json_body, timeout=20)
    if resp.status_code == 401:
        conf = cfg.load()
        conf = _refresh_token(conf)
        cfg.save(conf)
        resp = requests.post(url, headers=_headers(emit_fn),
                             params=params, json=json_body, timeout=20)
    return resp


def is_connected(emit=None) -> bool:
    def _log(msg):
        if emit:
            emit(f"[SP] {msg}")

    conf = cfg.load()
    if not conf["spotify"].get("enabled", False):
        _log("Not enabled — configure Spotify in Settings")
        return False
    if not conf["spotify"].get("refresh_token") and not conf["spotify"].get("access_token"):
        _log("No tokens stored — please connect Spotify in Settings")
        return False

    # Try to get a valid token (refreshing if necessary)
    try:
        token = _get_valid_token(emit_fn=emit)
    except RuntimeError as e:
        _log(str(e))
        return False

    # Quick probe
    try:
        resp = requests.get(f"{SPOTIFY_API_URL}/me",
                            headers={"Authorization": f"Bearer {token}"},
                            timeout=8)
        if resp.status_code == 401:
            # Refresh and retry one more time
            _log("Token probe returned 401 — attempting token refresh...")
            conf = _refresh_token(cfg.load())
            new_token = conf["spotify"].get("access_token", "")
            if not new_token:
                _log("Refresh failed — please reconnect Spotify in Settings")
                return False
            resp = requests.get(f"{SPOTIFY_API_URL}/me",
                                headers={"Authorization": f"Bearer {new_token}"},
                                timeout=8)
        if resp.ok:
            return True
        _log(f"Spotify API returned HTTP {resp.status_code} — skipping this sync")
        return False
    except Exception as e:
        _log(f"Could not reach Spotify API ({e}) — skipping this sync")
        return False


def get_user_id() -> str:
    resp = _api_get(f"{SPOTIFY_API_URL}/me")
    resp.raise_for_status()
    return resp.json()["id"]


def get_playlists() -> list[dict]:
    playlists, url = [], f"{SPOTIFY_API_URL}/me/playlists?limit=50"
    while url:
        resp = _api_get(url)
        resp.raise_for_status()
        data      = resp.json()
        playlists.extend([{"id": p["id"], "name": p["name"], "tracks": p["tracks"]["total"]}
                          for p in data.get("items", [])])
        url = data.get("next")
    return playlists


def _clean_playlist_id(playlist_id: str) -> str:
    return playlist_id.split("?")[0].strip()


def get_playlist_tracks(playlist_id: str) -> list[dict]:
    playlist_id = _clean_playlist_id(playlist_id)
    tracks, url = [], f"{SPOTIFY_API_URL}/playlists/{playlist_id}/tracks?limit=100"
    while url:
        resp = _api_get(url)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("items", []):
            track = item.get("track")
            if not track or track.get("is_local"):
                continue
            artists = ", ".join(a["name"] for a in track.get("artists", []))
            tracks.append({
                "artist":     artists,
                "title":      track["name"],
                "spotify_id": track["id"],
                "source":     "spotify",
            })
        url = data.get("next")
    return tracks


def search_track(artist: str, title: str) -> str | None:
    q    = f"track:{title} artist:{artist}"
    resp = _api_get(f"{SPOTIFY_API_URL}/search", params={"q": q, "type": "track", "limit": 5})
    if not resp.ok:
        return None
    items = resp.json().get("tracks", {}).get("items", [])
    return f"spotify:track:{items[0]['id']}" if items else None


def add_tracks_to_playlist(playlist_id: str, track_uris: list[str]):
    playlist_id = _clean_playlist_id(playlist_id)
    for i in range(0, len(track_uris), 100):
        chunk = track_uris[i:i+100]
        _api_post(f"{SPOTIFY_API_URL}/playlists/{playlist_id}/tracks",
                  json_body={"uris": chunk})


def create_playlist(name: str) -> str:
    uid  = get_user_id()
    resp = _api_post(f"{SPOTIFY_API_URL}/users/{uid}/playlists",
                     json_body={"name": name, "public": False})
    resp.raise_for_status()
    return resp.json()["id"]



