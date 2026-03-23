"""
Track metadata resolver.

Two jobs:
  1. parse_yt_title(channel, title) → {artist, title, clean}
     YouTube tracks often have the channel as "artist" and the real
     "Artist - Track Title (Official Video)" in the title field.
     We extract the real artist and clean title from the YT title string.

  2. resolve_album(artist, title, conf) → album_name or None
     When a track has no album metadata (common for YouTube-sourced tracks),
     try to find the album via:
       a) MusicBrainz recording search (artist + title → recording → release)
       b) Spotify search fallback (artist + title → track → album)
       c) YouTube Music search fallback (ytmusicapi song search → album)
"""

import re
import requests
import time
from app import config as cfg

# ── Patterns to strip from YouTube titles ──────────────────────────
_STRIP_SUFFIXES = re.compile(
    r'[\(\[]\s*('
    r'official\s*(music\s*)?video|official\s*audio|official\s*lyric\s*video|'
    r'lyrics?|lyric\s*video|audio|hq|hd|4k|full\s*version|extended\s*(mix|version)?|'
    r'radio\s*edit|original\s*(mix|version)|visuali[sz]er|remaster(?:ed)?|'
    r'music\s*video|official\s*clip|feat\..*|ft\..*|slowed.*|reverb.*|'
    r'\d{4}\s*remake|\d{4}\s*remaster'
    r')[\s\w]*[\)\]]',
    re.IGNORECASE
)

_STRIP_TRAILING = re.compile(
    r'\s*[\|\-]\s*(official\s*(music\s*)?video|official\s*audio|hd\s*remaster\w*|ministry of sound)\s*$',
    re.IGNORECASE
)

# Known YouTube channel names that are NOT the real artist
_CHANNEL_NOISE = {
    "armada music tv", "black hole recordings", "ministry of sound",
    "anjunabeats", "energy tv", "digital euphoria", "euphoriafeelings1",
    "artur wo.", "raaf123", "robmagic", "85kasiad85", "katsutrance",
    "chubby1izzie", "frozenflame", "oootinanaiiii", "allthingsian",
    "vnllaa", "pj80bass",
}


def parse_yt_title(channel: str, raw_title: str) -> dict:
    """
    Extract clean artist + title from a YouTube track.
    Returns dict with keys: artist, title, changed (bool)

    Handles:
      "Cosmic Gate - Exploration of Space [music video]"
        → artist="Cosmic Gate", title="Exploration of Space"
      "Veracocha - Carte Blanche (Official Music Video)"
        → artist="Veracocha", title="Carte Blanche"
      "ATB - You're Not Alone - HQ"
        → artist="ATB", title="You're Not Alone"
      "[HD] Paul van Dyk - For An Angel"
        → artist="Paul van Dyk", title="For An Angel"
      "deadmau5 — Ghosts 'n' Stuff (Radio Edit) (feat. Rob Swire)"
        → artist="deadmau5", title="Ghosts 'n' Stuff (feat. Rob Swire)"
          (Radio Edit stripped but feat. preserved)
    """
    channel_is_noise = channel.lower().strip() in _CHANNEL_NOISE

    # Strip leading [HD], [HQ] etc.
    raw = re.sub(r'^\s*\[(hd|hq|4k)\]\s*', '', raw_title, flags=re.IGNORECASE).strip()

    # Try to split on " - " (hyphen) or " — " (em-dash)
    sep_match = re.split(r'\s+[-—]\s+', raw, maxsplit=1)

    if len(sep_match) == 2:
        left, right = sep_match[0].strip(), sep_match[1].strip()

        # If channel is noise, left side is likely the real artist
        # If channel is a real artist name, use channel as artist and clean right side
        artist = left
        title  = right

        # Strip suffix noise from title
        title = _STRIP_SUFFIXES.sub('', title).strip()
        title = _STRIP_TRAILING.sub('', title).strip()
        title = title.strip(' -—').strip()

        changed = (artist != channel or title != raw_title)
        return {"artist": artist, "title": title, "changed": changed, "raw_title": raw_title}

    # No separator found — if channel is noise, we can't determine artist
    if channel_is_noise:
        return {"artist": "", "title": raw, "changed": False, "raw_title": raw_title}

    # Channel is probably the real artist, clean the title
    title = _STRIP_SUFFIXES.sub('', raw).strip()
    title = _STRIP_TRAILING.sub('', title).strip()
    return {"artist": channel, "title": title, "changed": True, "raw_title": raw_title}


# ── Album resolution ───────────────────────────────────────────────

_last_mb_call = 0.0

def _mb_recording_search(artist: str, title: str, conf: dict) -> str | None:
    """
    Search MusicBrainz recordings for artist+title, return the most likely
    album (release) name. Uses the recording endpoint, not artist→release-groups.
    Much better for finding individual track releases.
    """
    global _last_mb_call
    from app.importer import mb_auth, emit_debug, emit_warn, FUZZY_ARTIST_THRESHOLD
    from rapidfuzz import fuzz

    cooldown = conf["cooldowns"]["mb_cooldown"]
    elapsed  = time.time() - _last_mb_call
    if elapsed < cooldown:
        time.sleep(cooldown - elapsed)

    try:
        query   = f'recording:"{title}" AND artist:"{artist}"'
        headers = mb_auth.get_headers(conf)
        resp    = requests.get(
            "https://musicbrainz.org/ws/2/recording",
            params={"query": query, "limit": "10", "fmt": "json"},
            headers=headers, timeout=15
        )
        _last_mb_call = time.time()
        emit_debug(f"  MB recording search → HTTP {resp.status_code}")
        if not resp.ok:
            return None
        recordings = resp.json().get("recordings", [])
        emit_debug(f"  MB recording search returned {len(recordings)} result(s)")

        for rec in recordings:
            # Verify artist matches
            rec_artists = " ".join(a.get("name","") for a in rec.get("artist-credit", []))
            artist_score = fuzz.token_set_ratio(artist.lower(), rec_artists.lower())
            if artist_score < FUZZY_ARTIST_THRESHOLD:
                continue

            # Find best release (prefer studio albums over singles/compilations)
            releases = rec.get("releases", [])
            for rel in releases:
                rtype = rel.get("release-group", {}).get("primary-type", "")
                if rtype == "Album":
                    album_name = rel.get("title", "")
                    emit_debug(f"  MB recording → album '{album_name}' (type=Album)")
                    return album_name

            # Fallback: take first release of any type
            if releases:
                album_name = releases[0].get("title", "")
                emit_debug(f"  MB recording → release '{album_name}' (first available)")
                return album_name

        emit_debug(f"  MB recording search: no matching releases found")
        return None

    except Exception as e:
        emit_warn(f"  MB recording search failed: {e}")
        return None


def _spotify_album_lookup(artist: str, title: str, conf: dict) -> str | None:
    """Search Spotify for artist+title, return the album name."""
    from app.importer import emit_debug, emit_warn
    import app.spotify as spotify

    if not spotify.is_connected():
        return None
    try:
        q    = f"track:{title} artist:{artist}"
        resp = requests.get(
            "https://api.spotify.com/v1/search",
            headers=spotify._headers(),
            params={"q": q, "type": "track", "limit": 5},
            timeout=10
        )
        emit_debug(f"  Spotify album lookup → HTTP {resp.status_code}")
        if not resp.ok:
            return None
        items = resp.json().get("tracks", {}).get("items", [])
        if items:
            album = items[0].get("album", {}).get("name", "")
            emit_debug(f"  Spotify album lookup → '{album}'")
            return album or None
        return None
    except Exception as e:
        emit_warn(f"  Spotify album lookup failed: {e}")
        return None


def _ytmusic_album_lookup(artist: str, title: str) -> str | None:
    """Search ytmusicapi for artist+title, return album name."""
    from app.importer import emit_debug, emit_warn
    import app.youtube as youtube

    conf = cfg.load()
    if not youtube.is_connected() or conf["youtube"].get("method") != "headers":
        return None
    try:
        yt      = youtube._get_ytmusic()
        results = yt.search(f"{artist} {title}", filter="songs", limit=5)
        emit_debug(f"  YTMusic album lookup → {len(results)} result(s)")
        for r in results:
            album = r.get("album", {})
            if isinstance(album, dict):
                name = album.get("name", "")
            else:
                name = str(album) if album else ""
            if name:
                emit_debug(f"  YTMusic album lookup → '{name}'")
                return name
        return None
    except Exception as e:
        emit_warn(f"  YTMusic album lookup failed: {e}")
        return None


def resolve_album(artist: str, title: str, conf: dict) -> str | None:
    """
    Try to resolve the album for a track with unknown album metadata.
    Tries in order:
      1. MusicBrainz recording search
      2. Spotify track search (if connected)
      3. YouTube Music song search (if connected via headers)
    Returns album name or None.
    """
    from app.importer import emit_info, emit_warn, emit_debug

    emit_info(f"  Resolving album for: {artist} — {title}")

    # 1. MusicBrainz recording
    album = _mb_recording_search(artist, title, conf)
    if album:
        emit_info(f"  Album resolved via MusicBrainz recording: '{album}'")
        return album

    # 2. Spotify
    album = _spotify_album_lookup(artist, title, conf)
    if album:
        emit_info(f"  Album resolved via Spotify: '{album}'")
        return album

    # 3. YouTube Music
    album = _ytmusic_album_lookup(artist, title)
    if album:
        emit_info(f"  Album resolved via YouTube Music: '{album}'")
        return album

    emit_warn(f"  Could not resolve album for: {artist} — {title}")
    return None
