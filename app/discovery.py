"""
Discovery playlist sources.

Fetches fresh track recommendations from:
  - Spotify: read a specific user playlist (sync groups handle Spotify natively;
    Discover Weekly / Release Radar require extended quota mode — unavailable
    for standard dev-mode apps since Spotify's Nov 2024 API changes)
  - Last.fm: loved tracks, similar-artist discovery, tag/genre top tracks

Each function returns a list of {"artist": str, "title": str, "source": str}.
"""

import requests
from app import config as cfg

LASTFM_API = "https://ws.audioscrobbler.com/2.0/"


# ── Spotify — specific playlist only ─────────────────────────────
# Note: Discover Weekly and Release Radar are Spotify-internal playlists
# that require extended quota mode (unavailable for dev-mode apps since
# November 2024). The "Pick a specific playlist" option still works fine.

def get_spotify_specific_playlist(playlist_id: str, limit: int = 30) -> list[dict]:
    """Fetch tracks from a specific Spotify playlist by ID."""
    import app.spotify as spotify
    if not spotify.is_connected():
        print("[DISCOVERY] Spotify not connected — check Services tab")
        return []
    try:
        tracks = spotify.get_playlist_tracks(playlist_id)
        if not tracks:
            print(f"[DISCOVERY] Spotify returned 0 tracks for playlist '{playlist_id}'. "
                  f"If this is a Discover Weekly or Release Radar ID (starts with 37i9dQZEVXc), "
                  f"Spotify's API blocks access to algorithmic playlists for dev-mode apps. "
                  f"Use a playlist you own instead, or use Last.fm discovery.")
        else:
            print(f"[DISCOVERY] Fetched {len(tracks)} tracks from Spotify playlist '{playlist_id}'")
        return [{"artist": t["artist"], "title": t["title"],
                 "spotify_id": t.get("spotify_id", ""),
                 "source": "discovery_spotify"}
                for t in tracks[:limit]]
    except Exception as e:
        print(f"[DISCOVERY] Spotify playlist fetch error for '{playlist_id}': {e}")
        # Surface the HTTP status if available
        if hasattr(e, 'response') and e.response is not None:
            print(f"[DISCOVERY] HTTP {e.response.status_code}: {e.response.text[:300]}")
        return []


# ── Last.fm ───────────────────────────────────────────────────────

def _lfm_key(conf: dict) -> str:
    return conf.get("lastfm", {}).get("api_key", "")


def get_lastfm_loved_tracks(username: str, limit: int = 30) -> list[dict]:
    """
    Fetch tracks the user has explicitly loved/hearted in Last.fm.
    Hand-curated by the user — no session key needed.
    """
    conf    = cfg.load()
    api_key = _lfm_key(conf)
    if not api_key:
        print("[DISCOVERY] Last.fm API key not configured")
        return []
    try:
        resp = requests.get(LASTFM_API, params={
            "method":  "user.getLovedTracks",
            "user":    username,
            "api_key": api_key,
            "limit":   limit,
            "format":  "json",
        }, timeout=15)
        if resp.ok:
            items = resp.json().get("lovedtracks", {}).get("track", [])
            print(f"[DISCOVERY] Last.fm loved tracks: {len(items)} returned")
            return _lfm_tracks_to_list(items, limit)
        print(f"[DISCOVERY] Last.fm loved tracks HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[DISCOVERY] Last.fm loved tracks error: {e}")
    return []


def get_lastfm_similar_artist_tracks(username: str, limit: int = 30) -> list[dict]:
    """
    Discovery via similar artists:
    1. Fetch user's top artists (last 6 months)
    2. For each, call artist.getSimilar
    3. Pull top tracks from those similar artists
    Gives genuine "you might like" results. No session key needed.
    """
    conf    = cfg.load()
    api_key = _lfm_key(conf)
    if not api_key:
        print("[DISCOVERY] Last.fm API key not configured")
        return []

    # Step 1: user's top artists
    try:
        resp = requests.get(LASTFM_API, params={
            "method":  "user.getTopArtists",
            "user":    username,
            "api_key": api_key,
            "period":  "6month",
            "limit":   5,
            "format":  "json",
        }, timeout=15)
        if not resp.ok:
            print(f"[DISCOVERY] Last.fm top artists HTTP {resp.status_code}")
            return []
        top_artists = [a["name"] for a in resp.json().get("topartists", {}).get("artist", [])]
    except Exception as e:
        print(f"[DISCOVERY] Last.fm top artists error: {e}")
        return []

    if not top_artists:
        print("[DISCOVERY] No top artists found — listen to more music on Last.fm first")
        return []

    print(f"[DISCOVERY] Top artists for '{username}': {top_artists}")

    # Step 2: similar artists
    top_set = {a.lower() for a in top_artists}
    similar_artists = []
    for artist in top_artists:
        try:
            resp = requests.get(LASTFM_API, params={
                "method":  "artist.getSimilar",
                "artist":  artist,
                "api_key": api_key,
                "limit":   6,
                "format":  "json",
            }, timeout=15)
            if resp.ok:
                sims = [a["name"] for a in
                        resp.json().get("similarartists", {}).get("artist", [])]
                # Exclude artists the user already knows well
                similar_artists.extend(s for s in sims if s.lower() not in top_set)
        except Exception as e:
            print(f"[DISCOVERY] Similar artists error for '{artist}': {e}")

    # Deduplicate, preserve order
    seen, unique_similar = set(), []
    for a in similar_artists:
        if a.lower() not in seen:
            seen.add(a.lower())
            unique_similar.append(a)

    print(f"[DISCOVERY] Similar artists to explore: {unique_similar[:10]}")

    # Step 3: top tracks per similar artist
    results = []
    per_artist = max(2, limit // max(len(unique_similar), 1))
    for artist in unique_similar[:15]:
        if len(results) >= limit:
            break
        try:
            resp = requests.get(LASTFM_API, params={
                "method":  "artist.getTopTracks",
                "artist":  artist,
                "api_key": api_key,
                "limit":   per_artist,
                "format":  "json",
            }, timeout=15)
            if resp.ok:
                for t in resp.json().get("toptracks", {}).get("track", [])[:per_artist]:
                    title = t.get("name", "")
                    if artist and title:
                        results.append({"artist": artist, "title": title,
                                        "source": "discovery_lastfm"})
        except Exception as e:
            print(f"[DISCOVERY] Top tracks error for '{artist}': {e}")

    print(f"[DISCOVERY] Similar-artist discovery: {len(results)} tracks")
    return results[:limit]


def get_lastfm_tag_top_tracks(tag: str, limit: int = 30) -> list[dict]:
    """Fetch top tracks for a Last.fm tag/genre (e.g. 'trance', 'house')."""
    conf    = cfg.load()
    api_key = _lfm_key(conf)
    if not api_key:
        print("[DISCOVERY] Last.fm API key not configured")
        return []
    try:
        resp = requests.get(LASTFM_API, params={
            "method":  "tag.getTopTracks",
            "tag":     tag,
            "api_key": api_key,
            "limit":   limit,
            "format":  "json",
        }, timeout=15)
        if resp.ok:
            items = resp.json().get("tracks", {}).get("track", [])
            return _lfm_tracks_to_list(items, limit)
        print(f"[DISCOVERY] Last.fm tag HTTP {resp.status_code}")
    except Exception as e:
        print(f"[DISCOVERY] Last.fm tag tracks error: {e}")
    return []


def _lfm_tracks_to_list(items: list, limit: int) -> list[dict]:
    tracks = []
    for t in items[:limit]:
        artist = ""
        if isinstance(t.get("artist"), dict):
            artist = t["artist"].get("name", "")
        elif isinstance(t.get("artist"), str):
            artist = t["artist"]
        title = t.get("name", "")
        if artist and title:
            tracks.append({"artist": artist, "title": title,
                           "source": "discovery_lastfm"})
    return tracks


# ── Unified fetch ─────────────────────────────────────────────────

def fetch_discovery_tracks(group: dict) -> list[dict]:
    """
    Main entry point. Routes to the correct source based on group config.
    Returns a flat list of {"artist", "title", "source"} dicts.
    """
    source = group.get("discovery_source", "")
    limit  = int(group.get("discovery_limit", 30))

    def _lfm_username() -> str:
        u = group.get("lastfm_username", "").strip()
        if not u:
            u = cfg.load().get("lastfm", {}).get("username", "").strip()
        if not u:
            print("[DISCOVERY] Last.fm username not set — configure in Connections tab")
        return u

    if source == "spotify_playlist":
        pid = group.get("spotify_playlist_id", "").strip()
        if not pid:
            print("[DISCOVERY] spotify_playlist_id not set")
            return []
        return get_spotify_specific_playlist(pid, limit)

    elif source in ("spotify_discover_weekly", "spotify_release_radar"):
        # These require Spotify extended quota mode — not available for dev-mode apps
        # since November 2024. Inform user clearly.
        label = "Discover Weekly" if source == "spotify_discover_weekly" else "Release Radar"
        print(f"[DISCOVERY] {label}: Spotify removed API access to personalised playlists "
              f"for dev-mode apps in November 2024. Use 'Pick a specific playlist' instead.")
        return []

    elif source == "lastfm_loved":
        u = _lfm_username()
        return get_lastfm_loved_tracks(u, limit) if u else []

    elif source == "lastfm_similar":
        u = _lfm_username()
        return get_lastfm_similar_artist_tracks(u, limit) if u else []

    elif source in ("lastfm_recommended", "lastfm_tag"):
        if source == "lastfm_recommended":
            # Legacy value — remap to similar-artist discovery
            print("[DISCOVERY] lastfm_recommended remapped to similar-artist discovery")
            u = _lfm_username()
            return get_lastfm_similar_artist_tracks(u, limit) if u else []
        tag = group.get("lastfm_tag", "").strip()
        if not tag:
            print("[DISCOVERY] Last.fm tag not set")
            return []
        return get_lastfm_tag_top_tracks(tag, limit)

    else:
        print(f"[DISCOVERY] Unknown source: '{source}'")
        return []
