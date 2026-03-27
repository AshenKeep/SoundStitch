"""
Sync engine — union merges tracks from all platforms in a group,
pushes back to each platform, saves master playlist as CSV + M3U,
and forwards missing tracks to Lidarr.
"""

import csv
import io
import time
import re
import threading
from datetime import datetime
from queue import Queue
from pathlib import Path
from rapidfuzz import fuzz

from app import config as cfg
import app.spotify   as spotify
import app.youtube   as youtube
import app.importer  as importer
import app.notify    as notify
import app.discovery as discovery

MASTER_DIR = "/data/master"

def _forward_emit(level, msg):
    emit(level, msg)

importer.set_emit_override(_forward_emit)

_run_lock       = threading.Lock()
_is_running     = False
_stop_requested = False
_sync_thread    = None
progress_queue  = Queue()

# Stop event — defined here, shared with importer after creation
_stop_event = threading.Event()
importer._stop_event = _stop_event


# ═════════════════════════════════════════════════════════════════
#  PROGRESS
# ═════════════════════════════════════════════════════════════════

def emit(level: str, msg: str):
    print(f"[{level}] {msg}")
    progress_queue.put({"level": level, "msg": msg, "ts": datetime.now().strftime("%H:%M:%S")})

def emit_info(m):  emit("INFO",    m)
def emit_warn(m):  emit("WARN",    m)
def emit_error(m): emit("ERROR",   m)
def emit_ok(m):    emit("SUCCESS", m)
def emit_debug(m): emit("DEBUG",   m)


# ═════════════════════════════════════════════════════════════════
#  VERSION TYPE DETECTION
#  Detects whether a track is a live recording, acoustic version,
#  remix, demo, instrumental, remaster, edit, or cover.
#  Returns a dict with:
#    version_type:  "studio"|"live"|"acoustic"|"remix"|"demo"|
#                   "instrumental"|"remaster"|"edit"|"cover"
#    version_label: human-readable label extracted from title
#                   e.g. "Live at Brixton", "John 00 Fleming Remix"
#    remix_artist:  name of remix artist (if version_type=="remix")
#    clean_title:   title with noise stripped but version label preserved
#    base_title:    title with ALL version tags stripped (for MB search)
# ═════════════════════════════════════════════════════════════════

# Patterns that identify version type — checked in priority order
# Each entry: (version_type, regex that captures the version label group)
_VERSION_PATTERNS = [
    # Remix — most important for EDM. Capture the remixer name.
    ("remix",        re.compile(
        r'[\(\[]\s*((?:[^()\[\]]+?)\s+remix(?:\s+edit)?)\s*[\)\]]',
        re.IGNORECASE)),
    ("remix",        re.compile(
        r'[\(\[]\s*((?:[^()\[\]]+?)\s+(?:club|extended|radio|dub|vocal|instrumental)\s+mix)\s*[\)\]]',
        re.IGNORECASE)),
    ("remix",        re.compile(
        r'[\(\[]\s*((?:extended|club|radio|original|vocal|dub|instrumental)\s+(?:mix|version))\s*[\)\]]',
        re.IGNORECASE)),
    # Live
    ("live",         re.compile(
        r'[\(\[]\s*(live(?:\s+(?:at|from|in|@)\s+[^()\[\]]+?)?)\s*[\)\]]',
        re.IGNORECASE)),
    ("live",         re.compile(
        r'[\(\[]\s*(live(?:\s+version)?)\s*[\)\]]',
        re.IGNORECASE)),
    # Acoustic
    ("acoustic",     re.compile(
        r'[\(\[]\s*(acoustic(?:\s+(?:version|session|mix))?|unplugged(?:\s+version)?|mtv\s+unplugged)\s*[\)\]]',
        re.IGNORECASE)),
    # Demo
    ("demo",         re.compile(
        r'[\(\[]\s*(demo(?:\s+(?:version|recording|tape))?)\s*[\)\]]',
        re.IGNORECASE)),
    # Instrumental
    ("instrumental", re.compile(
        r'[\(\[]\s*(instrumental(?:\s+version)?)\s*[\)\]]',
        re.IGNORECASE)),
    # Remaster — preserve year if present
    ("remaster",     re.compile(
        r'[\(\[]\s*(\d{4}\s*(?:digital\s*)?remaster(?:ed)?(?:\s+version)?)\s*[\)\]]',
        re.IGNORECASE)),
    ("remaster",     re.compile(
        r'[\(\[]\s*(remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?)\s*[\)\]]',
        re.IGNORECASE)),
    # Cover / tribute
    ("cover",        re.compile(
        r'[\(\[]\s*(cover(?:\s+version)?|tribute)\s*[\)\]]',
        re.IGNORECASE)),
    # Single/radio edit
    ("edit",         re.compile(
        r'[\(\[]\s*((?:single|radio)\s+edit)\s*[\)\]]',
        re.IGNORECASE)),
]

# Pure noise — no version meaning, safe to strip
_NOISE_ONLY = re.compile(
    r'\s*[\(\[]\s*('
    r'official\s*(music\s*)?video|official\s*audio|official\s*lyric\s*video|'
    r'lyrics?\s*video?|audio\s*only|hq|hd|4k|'
    r'full\s*version|visuali[sz]er|music\s*video|official\s*clip|'
    r'slowed.*?|reverb.*?|\d{4}\s*remake|'
    r'feat\.?.*?'  # feat. tags — keep in base title search
    r')\s*[\)\]]',
    re.IGNORECASE
)
_NOISE_TRAILING = re.compile(
    r'\s*[\|\-]\s*(official\s*(music\s*)?video|official\s*audio|'
    r'hd\s*remaster\w*|ministry of sound|hq)\s*$',
    re.IGNORECASE
)
_LEADING_TAG = re.compile(r'^\s*\[(hd|hq|4k|official)\]\s*', re.IGNORECASE)


def detect_track_version(title: str) -> dict:
    """
    Analyse a track title and return version metadata.

    Returns dict with keys:
      version_type:  "studio"|"live"|"acoustic"|"remix"|"demo"|
                     "instrumental"|"remaster"|"edit"|"cover"
      version_label: extracted label string, e.g. "John 00 Fleming Remix"
      remix_artist:  remix artist name if version_type=="remix", else ""
      clean_title:   title with pure noise stripped, version label kept
      base_title:    title with ALL version/noise tags stripped (for MB search)
    """
    raw = title.strip()

    version_type  = "studio"
    version_label = ""
    remix_artist  = ""

    for vtype, pattern in _VERSION_PATTERNS:
        m = pattern.search(raw)
        if m:
            version_type  = vtype
            version_label = m.group(1).strip()
            if vtype == "remix":
                # Extract remixer — everything before "remix"/"mix"
                label_lower = version_label.lower()
                for suffix in (" remix edit", " remix", " club mix", " extended mix",
                                " radio mix", " dub mix", " vocal mix",
                                " instrumental mix", " original mix"):
                    if label_lower.endswith(suffix):
                        remix_artist = version_label[:len(version_label)-len(suffix)].strip()
                        break
                if not remix_artist:
                    remix_artist = version_label
            break

    # clean_title — strip only pure noise, preserve version labels
    clean = _LEADING_TAG.sub('', raw).strip()
    clean = _NOISE_ONLY.sub('', clean).strip()
    clean = _NOISE_TRAILING.sub('', clean).strip(' -—').strip()

    # base_title — strip everything including version tags (for MB search of base track)
    base = clean
    for _, pattern in _VERSION_PATTERNS:
        base = pattern.sub('', base).strip()
    base = _NOISE_ONLY.sub('', base).strip(' -—()[]').strip()

    return {
        "version_type":  version_type,
        "version_label": version_label,
        "remix_artist":  remix_artist,
        "clean_title":   clean,
        "base_title":    base or clean,
    }


def _clean_title(s: str) -> str:
    """Legacy clean for YT parser — strips noise only, preserves version labels."""
    s = _LEADING_TAG.sub('', s)
    s = _NOISE_ONLY.sub('', s)
    s = _NOISE_TRAILING.sub('', s)
    return s.strip(' -—').strip()


# ═════════════════════════════════════════════════════════════════
#  YOUTUBE TITLE PARSER
#  YouTube tracks often arrive with channel name as "artist" and
#  "Real Artist - Track Title (Official Video)" as the title.
#  We extract the real artist/title from the raw YT title string.
# ═════════════════════════════════════════════════════════════════


def parse_yt_track(artist: str, title: str) -> tuple[str, str]:
    """
    Given a raw YouTube artist (often channel name) and title
    (often "Real Artist - Track Title [noise]"), return (clean_artist, clean_title).

    Handles patterns like:
      channel="jurrgennn",  title="Cosmic Gate - Exploration of Space [music video]"
        → ("Cosmic Gate", "Exploration of Space")
      channel="Armada Music TV", title="Veracocha - Carte Blanche (Official Music Video)"
        → ("Veracocha", "Carte Blanche")
      channel="deadmau5", title="Ghosts 'n' Stuff (Radio Edit) (feat. Rob Swire)"
        → ("deadmau5", "Ghosts 'n' Stuff (feat. Rob Swire)")   ← keep feat., strip Radio Edit
      channel="Armin van Buuren", title="Airscape - L'Esperanza (Armin van Buuren Remix)"
        → ("Airscape", "L'Esperanza (Armin van Buuren Remix)")  ← title has "Artist - Track"
    """
    raw = title.strip()

    # Strip leading [HD] / [HQ] tags
    raw = _LEADING_TAG.sub('', raw).strip()

    # Try to split on " - " or " — "
    sep = re.split(r'\s+[-—]\s+', raw, maxsplit=1)

    if len(sep) == 2:
        left, right = sep[0].strip(), sep[1].strip()
        # left is the embedded artist in the title string
        clean_artist = left
        clean_title  = _clean_title(right)
        # If the embedded artist is suspiciously long (>40 chars) it's probably
        # a badly formatted title, not an artist name — fall back to channel
        if len(clean_artist) > 40:
            clean_artist = artist
            clean_title  = _clean_title(raw)
        return clean_artist, clean_title

    # No separator — use channel name as artist, clean the title
    return artist, _clean_title(raw)


def enrich_yt_tracks(tracks: list[dict]) -> list[dict]:
    """Parse YouTube tracks to extract real artist/title from video title strings,
    and detect version type (live, acoustic, remix etc.)."""
    enriched = []
    changed  = 0
    for t in tracks:
        orig_artist = t.get("artist", "")
        orig_title  = t.get("title",  "")
        new_artist, new_title = parse_yt_track(orig_artist, orig_title)

        if new_artist != orig_artist or new_title != orig_title:
            changed += 1
            emit_debug(f"  [YT-PARSE] '{orig_artist} — {orig_title}'"
                       f" → '{new_artist} — {new_title}'")

        # Detect version type from the cleaned title
        ver = detect_track_version(new_title)
        enriched.append({
            **t,
            "artist":        new_artist,
            "title":         new_title,
            "yt_raw_artist": orig_artist,
            "yt_raw_title":  orig_title,
            "version_type":  t.get("version_type") or ver["version_type"],
            "version_label": t.get("version_label") or ver["version_label"],
            "remix_artist":  t.get("remix_artist")  or ver["remix_artist"],
            "base_title":    t.get("base_title")     or ver["base_title"],
        })
    if changed:
        emit_info(f"[YT] Cleaned {changed}/{len(tracks)} track titles from video title strings")
    return enriched


# ═════════════════════════════════════════════════════════════════
#  ALBUM RESOLVER
#  When a track has no album (common for YouTube-sourced tracks),
#  try MusicBrainz recording search → Spotify → YouTube Music
# ═════════════════════════════════════════════════════════════════

_last_mb_resolve = 0.0


def _resolve_album_mb(artist: str, title: str, conf: dict,
                       version_type: str = "studio",
                       remix_artist: str = "") -> str | None:
    """
    Search MusicBrainz recordings for artist+title.
    Release ranking is biased by version_type:

      studio/remaster/edit/cover → prefer studio album > deluxe > single/EP
      live    → prefer live album > studio album > other
      acoustic→ prefer acoustic/unplugged single/EP > studio album
      demo    → prefer demo/EP > studio album
      remix   → prefer remix single (search uses remix_artist + title) > EP > album
      instrumental → prefer instrumental version recordings
    """
    global _last_mb_resolve  # must be declared before any use of the variable
    import re as _re

    # Compilation/promo/sampler name indicators — always reject
    _JUNK_PATTERNS = [
        r"^promo only[:\s]", r"^visions?[:\s]", r"^rock sound[:\s]",
        r"^alternative times[,\s]", r"^maximum metal[,\s]", r"^headshot\s",
        r"^by genre", r"^various artists", r"^\d{4}-\d{2}-\d{2}:",
        r"^\d{4}‐\d{2}‐\d{2}:", r"\bva\b\s*-", r"^triple j[:\s]",
        r"^dirt\s", r"systemfehler", r"original.*picture.*soundtrack",
        r"motion picture",
    ]

    def _junk_score(name: str) -> bool:
        nl = name.lower()
        return any(_re.search(pat, nl) for pat in _JUNK_PATTERNS)

    def _release_rank(rel: dict) -> int:
        """Score a MB release — ranking depends on version_type."""
        rg     = rel.get("release-group", {})
        ptype  = rg.get("primary-type", "")
        stypes = [s.lower() for s in rg.get("secondary-types", [])]
        name   = rel.get("title", "")
        nl     = name.lower()

        if _junk_score(name):
            return 0

        is_live         = "live"         in stypes
        is_compilation  = "compilation"  in stypes
        is_soundtrack   = "soundtrack"   in stypes
        is_demo         = "demo"         in stypes
        is_remix_ep     = ("remix" in nl or "remixes" in nl)
        is_acoustic_rel = ("acoustic" in nl or "unplugged" in nl)
        is_single_ep    = ptype in ("Single", "EP")
        is_album        = ptype == "Album"

        if version_type == "live":
            if is_live:                         return 7
            if is_album and not is_compilation: return 3  # studio fallback
            if is_single_ep:                    return 2
            return 1

        elif version_type == "acoustic":
            if is_acoustic_rel:                 return 7
            if is_single_ep and is_acoustic_rel:return 8
            if is_single_ep:                    return 4  # acoustic singles
            if is_album and not is_compilation: return 3
            return 1

        elif version_type == "demo":
            if is_demo:                         return 7
            if is_single_ep:                    return 5
            if is_album and not is_compilation: return 3
            return 1

        elif version_type == "remix":
            if is_remix_ep and is_single_ep:    return 8
            if is_remix_ep:                     return 7
            if is_single_ep:                    return 5
            if is_album and not is_compilation: return 3
            return 1

        elif version_type == "instrumental":
            if "instrumental" in nl:            return 7
            if is_single_ep:                    return 5
            if is_album and not is_compilation: return 3
            return 1

        else:
            # studio / remaster / edit / cover / default — original ranking
            if _junk_score(name):               return 0
            if not is_album:
                if is_single_ep:                return 3
                return 1
            if is_compilation:                  return 1
            if is_live:                         return 2
            if is_soundtrack or is_demo:        return 1
            if any(x in nl for x in ("deluxe", "special edition", "expanded", "anniversary")):
                return 4
            return 5

    # For remixes, build a search query using remix_artist + title
    if version_type == "remix" and remix_artist:
        search_title  = title  # keep full title with remix label for MB
        search_artist = artist
    else:
        search_title  = title
        search_artist = artist

    rec_key = f"recording:{version_type}:{search_artist.lower()}|{search_title.lower()}"
    cached  = importer._mb_cache_get(rec_key)
    if cached is not None:
        emit_debug(f"  [MB-REC] Cache hit ({version_type}) — album: '{cached}'")
        return cached or None

    cooldown = conf["cooldowns"]["mb_cooldown"]
    elapsed  = time.time() - _last_mb_resolve
    if elapsed < cooldown:
        importer._interruptible_sleep(cooldown - elapsed)

    try:
        headers = importer.mb_auth.get_headers(conf)
        query   = f'recording:"{search_title}" AND artist:"{search_artist}"'
        resp    = importer.requests.get(
            f"{importer.MB_BASE_URL}/recording",
            params={"query": query, "limit": "10", "fmt": "json",
                    "inc": "releases+release-groups"},
            headers=headers, timeout=15
        )
        _last_mb_resolve = time.time()
        emit_debug(f"  [MB-REC] recording search ({version_type}) → HTTP {resp.status_code}")
        if not resp.ok:
            return None

        recordings = resp.json().get("recordings", [])
        emit_debug(f"  [MB-REC] {len(recordings)} recording(s) found")

        best_name = None
        best_rank = -1
        # For live/acoustic/remix — we can do better than 5 (studio max)
        perfect   = 8 if version_type in ("live","acoustic","demo","remix","instrumental") else 5

        for rec in recordings:
            rec_artists = " ".join(
                a.get("name", "") for a in rec.get("artist-credit", [])
                if isinstance(a, dict)
            )
            if fuzz.token_set_ratio(search_artist.lower(), rec_artists.lower()) < 60:
                continue

            for rel in rec.get("releases", []):
                rank = _release_rank(rel)
                name = rel.get("title", "")
                if not name:
                    continue
                emit_debug(f"  [MB-REC] Release '{name}' rank={rank} (want: {version_type})")
                if rank > best_rank:
                    best_rank = rank
                    best_name = name
                if best_rank >= perfect:
                    break

            if best_rank >= perfect:
                break

        if best_name:
            tag = {8:"Perfect match", 7:"Good match", 5:"Studio Album",
                   4:"Deluxe Edition", 3:"Single/EP", 2:"Live",
                   1:"Other", 0:"Junk"}.get(best_rank, "?")
            emit_debug(f"  [MB-REC] Best ({tag}, wanted {version_type}): '{best_name}'")
            importer._mb_cache_set(rec_key, best_name)
            return best_name

    except Exception as e:
        emit_debug(f"  [MB-REC] Exception: {e}")

    importer._mb_cache_set(rec_key, "")
    return None
    """
    Search MusicBrainz recordings for artist+title.
    Returns the best album name, preferring proper studio releases over
    compilations, promos, samplers, and live bootlegs.

    Release quality ranking (higher = better):
      5 — Official studio album (no secondary types, no promo/sampler indicators)
      4 — Deluxe / special / expanded edition of a studio album
      3 — Single or EP
      2 — Live album
      1 — Compilation, soundtrack, or other
      0 — Promo/sampler/various-artists (filtered by name patterns)

    rec_key is defined BEFORE the try block so it is in scope for both
    the cache-set calls inside the try AND the negative-result set after it.
    """
    # Compilation/promo/sampler name indicators — reject these even if tagged "Album"
    _JUNK_PATTERNS = [
        r"^promo only[:\s]",
        r"^visions?[:\s]",
        r"^rock sound[:\s]",
        r"^alternative times[,\s]",
        r"^maximum metal[,\s]",
        r"^headshot\s",
        r"^by genre",
        r"^various artists",
        r"^\d{4}-\d{2}-\d{2}:",      # live bootleg date strings
        r"^\d{4}‐\d{2}‐\d{2}:",      # unicode dashes
        r"\bva\b\s*-",
        r"^triple j[:\s]",
        r"^dirt\s",                   # DiRT game soundtracks
        r"systemfehler",
        r"original.*picture.*soundtrack",
        r"motion picture",
    ]
    import re as _re

    def _junk_score(name: str) -> bool:
        """Return True if this release name looks like a promo/sampler/various."""
        nl = name.lower()
        for pat in _JUNK_PATTERNS:
            if _re.search(pat, nl):
                return True
        return False

    def _release_rank(rel: dict) -> int:
        """Score a MB release dict — higher is better."""
        rg   = rel.get("release-group", {})
        ptype = rg.get("primary-type", "")
        stypes = [s.lower() for s in rg.get("secondary-types", [])]
        name  = rel.get("title", "")

        if _junk_score(name):
            return 0

        if ptype != "Album":
            if ptype in ("Single", "EP"):
                return 3
            if ptype == "Broadcast":
                return 1
            return 1  # Other/unknown

        # It's tagged Album — check secondary types
        if "compilation" in stypes:
            return 1
        if "live" in stypes:
            return 2
        if "soundtrack" in stypes:
            return 1
        if "mixtape/street" in stypes or "demo" in stypes:
            return 1

        # Clean studio album — check for deluxe/special editions
        nl = name.lower()
        if any(x in nl for x in ("deluxe", "special edition", "expanded", "anniversary")):
            return 4

        return 5  # Proper studio album

    rec_key = f"recording:{artist.lower()}|{title.lower()}"
    cached  = importer._mb_cache_get(rec_key)
    if cached is not None:
        emit_debug(f"  [MB-REC] Cache hit — album: '{cached}'")
        return cached or None

    cooldown = conf["cooldowns"]["mb_cooldown"]
    elapsed  = time.time() - _last_mb_resolve
    if elapsed < cooldown:
        importer._interruptible_sleep(cooldown - elapsed)

    try:
        headers = importer.mb_auth.get_headers(conf)
        query   = f'recording:"{title}" AND artist:"{artist}"'
        resp    = importer.requests.get(
            f"{importer.MB_BASE_URL}/recording",
            params={"query": query, "limit": "10", "fmt": "json",
                    "inc": "releases+release-groups"},
            headers=headers, timeout=15
        )
        _last_mb_resolve = time.time()
        emit_debug(f"  [MB-REC] recording search → HTTP {resp.status_code}")
        if not resp.ok:
            return None

        recordings = resp.json().get("recordings", [])
        emit_debug(f"  [MB-REC] {len(recordings)} recording(s) found")

        best_name  = None
        best_rank  = -1

        for rec in recordings:
            rec_artists = " ".join(
                a.get("name", "") for a in rec.get("artist-credit", [])
                if isinstance(a, dict)
            )
            if fuzz.token_set_ratio(artist.lower(), rec_artists.lower()) < 60:
                continue

            for rel in rec.get("releases", []):
                rank = _release_rank(rel)
                name = rel.get("title", "")
                if not name:
                    continue
                emit_debug(f"  [MB-REC] Release '{name}' rank={rank}")
                if rank > best_rank:
                    best_rank = rank
                    best_name = name
                if best_rank == 5:
                    break  # Can't do better than a clean studio album

            if best_rank == 5:
                break

        if best_name:
            tag = {5:"Studio Album", 4:"Deluxe Edition", 3:"Single/EP",
                   2:"Live", 1:"Compilation/Other", 0:"Promo/Sampler"}.get(best_rank, "?")
            emit_debug(f"  [MB-REC] Best release ({tag}): '{best_name}'")
            importer._mb_cache_set(rec_key, best_name)
            return best_name

    except Exception as e:
        emit_debug(f"  [MB-REC] Exception: {e}")

    importer._mb_cache_set(rec_key, "")
    return None


def _resolve_album_spotify(artist: str, title: str,
                            version_type: str = "studio",
                            remix_artist: str = "") -> str | None:
    """Search Spotify for track, return album name biased by version_type."""
    if not spotify.is_connected():
        return None
    try:
        # Build queries — remix uses remix_artist for better matching
        if version_type == "remix" and remix_artist:
            queries = [f"track:{title} artist:{remix_artist}",
                       f"track:{title} artist:{artist}",
                       f"{artist} {title}"]
        else:
            queries = [f"track:{title} artist:{artist}", f"{artist} {title}"]

        for q in queries:
            resp = importer.requests.get(
                "https://api.spotify.com/v1/search",
                headers=spotify._headers(),
                params={"q": q, "type": "track", "limit": 5},
                timeout=10
            )
            if not resp.ok:
                continue
            items = resp.json().get("tracks", {}).get("items", [])
            for item in items:
                item_artists = ", ".join(a["name"] for a in item.get("artists", []))
                check_artist = remix_artist if (version_type=="remix" and remix_artist) else artist
                if fuzz.token_set_ratio(check_artist.lower(), item_artists.lower()) < 55:
                    continue
                album      = item.get("album", {}).get("name", "")
                album_type = item.get("album", {}).get("album_type", "")
                album_nl   = album.lower()

                # For live/acoustic/remix — prefer albums that match the version type
                if version_type == "live" and "live" in album_nl:
                    emit_debug(f"  [SP-ALB] Spotify live album: '{album}'")
                    return album
                if version_type == "acoustic" and ("acoustic" in album_nl or "unplugged" in album_nl):
                    emit_debug(f"  [SP-ALB] Spotify acoustic album: '{album}'")
                    return album
                if version_type == "remix" and ("remix" in album_nl or "remixes" in album_nl):
                    emit_debug(f"  [SP-ALB] Spotify remix album: '{album}'")
                    return album

                if album and album_type != "single":
                    emit_debug(f"  [SP-ALB] Spotify album ({album_type}): '{album}'")
                    return album
                elif album:
                    emit_debug(f"  [SP-ALB] Spotify single: '{album}'")
                    return album
    except Exception as e:
        emit_debug(f"  [SP-ALB] Exception: {e}")
    return None


def _resolve_album_ytmusic(artist: str, title: str) -> str | None:
    """Search YTMusic for song, return album name."""
    conf = cfg.load()
    if not youtube.is_connected() or conf["youtube"].get("method") != "headers":
        return None
    try:
        yt      = youtube._get_ytmusic()
        results = yt.search(f"{artist} {title}", filter="songs", limit=5)
        for r in results:
            alb = r.get("album")
            if isinstance(alb, dict):
                name = alb.get("name", "")
            else:
                name = str(alb) if alb else ""
            if name:
                emit_debug(f"  [YT-ALB] YTMusic album: '{name}'")
                return name
    except Exception as e:
        emit_debug(f"  [YT-ALB] Exception: {e}")
    return None


def resolve_album_for_track(artist: str, title: str, conf: dict,
                             version_type: str = "studio",
                             remix_artist: str = "") -> str | None:
    """
    Try to resolve a missing album name for a track.
    Chain: MusicBrainz recording search → Spotify → YouTube Music
    version_type biases each resolver to prefer the right kind of release.
    """
    emit_info(f"  [ALB-RESOLVE] Looking up album for: {artist} — {title}"
              + (f" [{version_type}]" if version_type != "studio" else "")
              + (f" (remix by {remix_artist})" if remix_artist else ""))

    album = _resolve_album_mb(artist, title, conf,
                               version_type=version_type, remix_artist=remix_artist)
    if album:
        emit_ok(f"  [ALB-RESOLVE] Found via MusicBrainz: '{album}'")
        return album

    album = _resolve_album_spotify(artist, title,
                                    version_type=version_type, remix_artist=remix_artist)
    if album:
        emit_ok(f"  [ALB-RESOLVE] Found via Spotify: '{album}'")
        return album

    album = _resolve_album_ytmusic(artist, title)
    if album:
        emit_ok(f"  [ALB-RESOLVE] Found via YouTube Music: '{album}'")
        return album

    emit_warn(f"  [ALB-RESOLVE] Could not find album for: {artist} — {title}")
    return None


def tag_track_versions(tracks: list[dict]) -> list[dict]:
    """
    Detect and tag version_type/version_label/remix_artist/base_title
    on any track list that hasn't been through enrich_yt_tracks.
    Only fills in blanks — never overwrites existing tags.
    """
    for t in tracks:
        if t.get("version_type"):
            continue  # already tagged
        title = t.get("title", "")
        if not title:
            continue
        ver = detect_track_version(title)
        t["version_type"]  = ver["version_type"]
        t["version_label"] = ver["version_label"]
        t["remix_artist"]  = ver["remix_artist"]
        t["base_title"]    = ver["base_title"]
        if ver["version_type"] != "studio":
            emit_debug(f"  [VER] '{title}' → {ver['version_type']}"
                       + (f" ({ver['version_label']})" if ver["version_label"] else ""))
    return tracks


def enrich_missing_albums(tracks: list[dict], conf: dict) -> list[dict]:
    """
    For tracks that have a real artist but no album,
    attempt to resolve the album via MB/Spotify/YTMusic.
    Tracks with metadata_override=True are skipped — their metadata is authoritative.
    version_type on each track biases the resolver to find the right kind of release.
    Modifies tracks in place and returns them.
    """
    no_album = [t for t in tracks
                if not t.get("album", "").strip() and not t.get("metadata_override")]
    if not no_album:
        return tracks

    emit_info(f"[ALB-RESOLVE] {len(no_album)} track(s) have no album — attempting resolution...")
    resolved = 0
    for t in no_album:
        if _stop_requested:
            emit_warn("[ALB-RESOLVE] Stop requested — aborting album resolution")
            break
        artist       = t.get("artist",       "").strip()
        title        = t.get("title",        "").strip()
        version_type = t.get("version_type", "studio") or "studio"
        remix_artist = t.get("remix_artist", "") or ""
        # Use base_title for studio tracks so version suffixes don't confuse MB search
        search_title = t.get("base_title", title) if version_type == "studio" else title
        if not artist or not title:
            continue
        album = resolve_album_for_track(artist, search_title, conf,
                                        version_type=version_type,
                                        remix_artist=remix_artist)
        if album:
            t["album"] = album
            resolved += 1

    emit_info(f"[ALB-RESOLVE] Resolved {resolved}/{len(no_album)} album(s)")
    return tracks


# ═════════════════════════════════════════════════════════════════
#  TRACK DEDUPLICATION (union merge)
# ═════════════════════════════════════════════════════════════════

def _tracks_match(a: dict, b: dict, threshold: int = 85) -> bool:
    """
    True if two track dicts refer to the same recording.
    Version-typed tracks (live, acoustic, remix etc.) are only considered
    the same if their version_type AND version_label both match.
    Studio vs untagged are treated as equivalent (default assumption).
    """
    vt_a = a.get("version_type") or "studio"
    vt_b = b.get("version_type") or "studio"

    # If both have explicit (non-studio) version types they must match
    if vt_a != "studio" and vt_b != "studio":
        if vt_a != vt_b:
            return False
        # For remixes: also require remix_artist to match
        if vt_a == "remix":
            ra_a = (a.get("remix_artist") or "").lower().strip()
            ra_b = (b.get("remix_artist") or "").lower().strip()
            if ra_a and ra_b and fuzz.token_set_ratio(ra_a, ra_b) < 80:
                return False

    score = max(
        fuzz.token_sort_ratio(
            f"{a.get('artist','')} {a.get('title','')}".lower(),
            f"{b.get('artist','')} {b.get('title','')}".lower()),
        fuzz.token_set_ratio(
            f"{a.get('artist','')} {a.get('title','')}".lower(),
            f"{b.get('artist','')} {b.get('title','')}".lower()),
    )
    return score >= threshold


def union_merge(track_lists: list[list[dict]],
                blacklist: list[dict] | None = None) -> list[dict]:
    """
    Merge multiple track lists into a deduplicated master.
    Tracks matching any entry in blacklist (by artist+title fuzzy match) are excluded.
    """
    def _is_blacklisted(track: dict) -> bool:
        if not blacklist:
            return False
        artist = track.get("artist", "").lower().strip()
        title  = track.get("title",  "").lower().strip()
        for b in blacklist:
            ba = b.get("artist", "").lower().strip()
            bt = b.get("title",  "").lower().strip()
            if (fuzz.token_set_ratio(artist, ba) >= 90 and
                    fuzz.token_set_ratio(title, bt) >= 90):
                return True
        return False

    merged: list[dict] = []
    for tracks in track_lists:
        for track in tracks:
            if _is_blacklisted(track):
                continue
            found = False
            for existing in merged:
                if _tracks_match(track, existing):
                    for k, v in track.items():
                        if k not in existing or not existing[k]:
                            existing[k] = v
                        elif k == "source":
                            sources = set(existing["source"].split(",")) | {v}
                            existing["source"] = ",".join(sources)
                    found = True
                    break
            if not found:
                merged.append(dict(track))
    return merged


# ═════════════════════════════════════════════════════════════════
#  MASTER PLAYLIST — CSV + M3U
# ═════════════════════════════════════════════════════════════════

def save_master_snapshot(group: dict, tracks: list[dict]):
    """
    Save merged master playlist as:
      data/master/<group_id>.csv       (latest, overwritten each sync)
      data/master/<group_id>.m3u8      (latest m3u, overwritten each sync)
      data/master/<group_id>/<ts>.csv  (timestamped history, last 10 kept)
    """
    try:
        master_dir = Path(MASTER_DIR)
        master_dir.mkdir(parents=True, exist_ok=True)

        ts      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ts_file = datetime.now().strftime("%Y%m%d_%H%M%S")

        # ── Build CSV ──────────────────────────────────────────────
        csv_fields = ["#", "artist", "title", "album", "source",
                      "navidrome_id", "spotify_id", "youtube_id",
                      "mb_artist_id", "mb_album_id",
                      "version_type", "version_label", "remix_artist", "base_title",
                      "flagged", "metadata_override"]
        csv_rows = []
        for i, t in enumerate(tracks, 1):
            csv_rows.append({
                "#":                i,
                "artist":           t.get("artist",            ""),
                "title":            t.get("title",              ""),
                "album":            t.get("album",              ""),
                "source":           t.get("source",             ""),
                "navidrome_id":     t.get("navidrome_id",       ""),
                "spotify_id":       t.get("spotify_id",         ""),
                "youtube_id":       t.get("youtube_id",         ""),
                "mb_artist_id":     t.get("mb_artist_id",       ""),
                "mb_album_id":      t.get("mb_album_id",        ""),
                "version_type":     t.get("version_type",       ""),
                "version_label":    t.get("version_label",      ""),
                "remix_artist":     t.get("remix_artist",       ""),
                "base_title":       t.get("base_title",         ""),
                "flagged":          "1" if t.get("flagged") else "",
                "metadata_override":"1" if t.get("metadata_override") else "",
            })

        def _write_csv(path: Path):
            with open(path, "w", newline="", encoding="utf-8") as f:
                # Metadata comment MUST come before the header row so that
                # csv.DictReader uses the correct first non-comment line as headers
                f.write(f"# Group: {group.get('name')} | Saved: {ts} | Tracks: {len(tracks)}\n")
                writer = csv.DictWriter(f, fieldnames=csv_fields)
                writer.writeheader()
                writer.writerows(csv_rows)

        latest_csv = master_dir / f"{group['id']}.csv"
        _write_csv(latest_csv)

        # ── Build M3U ──────────────────────────────────────────────
        def _write_m3u(path: Path):
            lines = ["#EXTM3U", f"#PLAYLIST:{group.get('name')} (saved {ts})"]
            for t in tracks:
                dur    = -1
                artist = t.get("artist", "")
                title  = t.get("title",  "")
                album  = t.get("album",  "")
                mb_meta = ""
                if t.get("mb_artist_id"): mb_meta += f" mb_artist={t['mb_artist_id']}"
                if t.get("mb_album_id"):  mb_meta += f" mb_album={t['mb_album_id']}"
                lines.append(f"#EXTINF:{dur},{artist} - {title}")
                if mb_meta: lines.append(f"#EXTSOUNDSTITCH:{mb_meta.strip()}")
                # Best available path reference
                if t.get("navidrome_id"):
                    lines.append(f"navidrome://{t['navidrome_id']}")
                elif t.get("spotify_id"):
                    lines.append(f"spotify:track:{t['spotify_id']}")
                elif t.get("youtube_id"):
                    lines.append(f"https://music.youtube.com/watch?v={t['youtube_id']}")
                else:
                    lines.append(f"{artist}/{album}/{title}")
            path.write_text("\n".join(lines), encoding="utf-8")

        latest_m3u = master_dir / f"{group['id']}.m3u8"
        _write_m3u(latest_m3u)

        # ── Timestamped CSV history ────────────────────────────────
        hist_dir  = master_dir / group["id"]
        hist_dir.mkdir(exist_ok=True)
        hist_csv  = hist_dir / f"{ts_file}.csv"
        _write_csv(hist_csv)

        # Prune: keep last 10 history files
        hist_files = sorted(hist_dir.glob("*.csv"), reverse=True)
        for old in hist_files[10:]:
            old.unlink()

        emit_ok(f"Master saved: {latest_csv.name} + {latest_m3u.name} ({len(tracks)} tracks)")

    except Exception as e:
        emit_warn(f"Could not save master snapshot: {e}")


def _load_cached_fields_from_snapshot(group_id: str, tracks: list[dict]):
    """
    Read the latest saved CSV and restore previously resolved fields onto
    matching tracks by (artist_lower, title_lower) key. Fields restored:
      album, spotify_id, youtube_id, mb_artist_id, mb_album_id, flagged, metadata_override
    Only fills in blanks — never overwrites a value the current fetch already provided.
    Tracks with metadata_override=True have their artist/title/album locked — the cached
    values are authoritative and will NOT be overwritten by future source fetches.
    """
    csv_path = Path(MASTER_DIR) / f"{group_id}.csv"
    if not csv_path.exists():
        return
    try:
        import csv as _csv
        content    = csv_path.read_text(encoding="utf-8")
        data_lines = []
        for line in content.splitlines():
            if line.startswith("# ") or (line.startswith("#") and not line.startswith("#,")):
                continue
            if line.strip():
                data_lines.append(line)

        stored = {}
        for row in _csv.DictReader(data_lines):
            a = row.get("artist", "").strip().lower()
            t = row.get("title",  "").strip().lower()
            if a and t:
                stored[(a, t)] = {
                    "album":             row.get("album",             "").strip(),
                    "spotify_id":        row.get("spotify_id",        "").strip(),
                    "youtube_id":        row.get("youtube_id",        "").strip(),
                    "mb_artist_id":      row.get("mb_artist_id",      "").strip(),
                    "mb_album_id":       row.get("mb_album_id",       "").strip(),
                    "version_type":      row.get("version_type",      "").strip(),
                    "version_label":     row.get("version_label",     "").strip(),
                    "remix_artist":      row.get("remix_artist",      "").strip(),
                    "base_title":        row.get("base_title",        "").strip(),
                    "flagged":           row.get("flagged",           "").strip() == "1",
                    "metadata_override": row.get("metadata_override", "").strip() == "1",
                    "artist_override":   row.get("artist", "").strip(),
                    "title_override":    row.get("title",  "").strip(),
                }
        if not stored:
            emit_debug(f"[CACHE] No usable rows in snapshot for group {group_id}")
            return
        loaded = 0
        for track in tracks:
            key = (track.get("artist","").strip().lower(),
                   track.get("title", "").strip().lower())
            if key not in stored:
                continue
            prev    = stored[key]
            changed = False
            for field in ("album", "spotify_id", "youtube_id", "mb_artist_id", "mb_album_id"):
                if prev.get(field) and not track.get(field):
                    track[field] = prev[field]
                    changed = True
            # Restore version fields — only fill blanks, don't overwrite fresh detection
            for field in ("version_type", "version_label", "remix_artist", "base_title"):
                if prev.get(field) and not track.get(field):
                    track[field] = prev[field]
                    changed = True
            # Restore flags — always (they don't come from source)
            if prev.get("flagged"):
                track["flagged"] = True
                changed = True
            if prev.get("metadata_override"):
                track["metadata_override"] = True
                track["artist"] = prev["artist_override"]
                track["title"]  = prev["title_override"]
                if prev.get("album"):
                    track["album"] = prev["album"]
                # Also restore locked version_type if present
                if prev.get("version_type"):
                    track["version_type"]  = prev["version_type"]
                    track["version_label"] = prev.get("version_label", "")
                    track["remix_artist"]  = prev.get("remix_artist",  "")
                changed = True
            if changed:
                loaded += 1
        emit_debug(f"[CACHE] Restored cached metadata for {loaded}/{len(tracks)} track(s)")
    except Exception as e:
        emit_debug(f"[CACHE] Could not load snapshot: {e}")


# ═════════════════════════════════════════════════════════════════
#  NAVIDROME HELPERS
# ═════════════════════════════════════════════════════════════════

def _get_navidrome_tracks(playlist_name: str, conf: dict) -> list[dict]:
    existing = importer.get_existing_playlist(playlist_name, conf)
    if not existing:
        emit_info(f"  Navidrome playlist '{playlist_name}' does not exist yet — will be created")
        return []
    try:
        data   = importer.nd_get("getPlaylist", conf, {"id": existing["id"]})
        tracks = data.get("playlist", {}).get("entry", [])
        return [{
            "artist":       t.get("artist", ""),
            "title":        t.get("title",  ""),
            "album":        t.get("album",  ""),
            "navidrome_id": t.get("id",     ""),
            "source":       "navidrome",
        } for t in tracks]
    except Exception as e:
        emit_warn(f"Could not fetch Navidrome playlist tracks: {e}")
        return []


# ═════════════════════════════════════════════════════════════════
#  DISCOVERY GROUP SYNC
#  Fetches fresh recommendations, enriches metadata (same pipeline
#  as sync_group Phases 3-4), then wipes/replaces the ND playlist
#  and sends missing tracks to Lidarr (Phases 7-8).
#  Does NOT push back to Spotify/YouTube (read-only from those).
# ═════════════════════════════════════════════════════════════════

def sync_discovery_group(group: dict, conf: dict) -> dict:
    name = group.get("name", "Discovery")
    emit_info("═" * 50)
    emit_info(f"DISCOVERY: '{name}'")
    emit_info("═" * 50)

    nd_playlist = group.get("discovery_nd_playlist") or name
    if group.get("_nd_offline"):
        nd_playlist = ""

    _stats = {"added_sp": 0, "added_yt": 0, "added_nd": 0, "missing": 0, "errors": 0}

    # ════════════════════════════════════════════════
    # PHASE 1 — FETCH from discovery source
    # ════════════════════════════════════════════════
    source = group.get("discovery_source", "?")
    emit_info(f"[DISC] Fetching from source: {source}")
    if source == "spotify_playlist":
        pid = group.get("spotify_playlist_id", "")
        emit_info(f"[DISC] Spotify playlist ID: {pid}")

    tracks = discovery.fetch_discovery_tracks(group)

    if not tracks:
        if source == "spotify_playlist":
            pid = group.get("spotify_playlist_id", "")
            if pid.startswith("37i9dQZEVXc"):
                emit_warn("[DISC] Spotify returned 0 tracks — this ID looks like a Spotify-generated "
                          "algorithmic playlist (Discover Weekly / Release Radar). Spotify blocks "
                          "access to these for dev-mode apps since Nov 2024. Try a playlist you own.")
            else:
                emit_warn(f"[DISC] Spotify returned 0 tracks for playlist '{pid}' — "
                          f"check the ID is correct and the playlist is public or owned by your account.")
        else:
            emit_warn("[DISC] No tracks returned from discovery source — aborting")
        return {"name": name, **_stats}

    emit_ok(f"[DISC] {len(tracks)} track(s) fetched from discovery source")
    if _stop_requested:
        return {"name": name, **_stats}

    # ════════════════════════════════════════════════
    # PHASE 2 — RESTORE cached metadata from previous snapshot
    # Fills in album, mb_artist_id, mb_album_id from last run
    # so we don't re-resolve everything on every sync.
    # ════════════════════════════════════════════════
    _load_cached_fields_from_snapshot(group.get("id", ""), tracks)

    # ════════════════════════════════════════════════
    # PHASE 3 — ENRICH missing album metadata
    # MB recording search → Spotify search → YTMusic search
    # Lidarr needs the album name to find the right release.
    # ════════════════════════════════════════════════
    needs_album = [t for t in tracks if not t.get("album", "").strip()]
    if needs_album:
        emit_info(f"[DISC] Resolving album metadata for {len(needs_album)} track(s) without album info...")
        enrich_missing_albums(needs_album, conf)
        resolved = sum(1 for t in needs_album if t.get("album", "").strip())
        emit_info(f"[DISC] Album resolution: {resolved}/{len(needs_album)} resolved")
    else:
        emit_debug("[DISC] All tracks already have album info — skipping album resolution")

    if _stop_requested:
        return {"name": name, **_stats}

    # Save snapshot with full metadata before any further processing
    save_master_snapshot(group, tracks)

    # ════════════════════════════════════════════════
    # PHASE 4 — SEARCH Navidrome and build playlist
    # Clear-and-replace: wipes the ND playlist each run.
    # ════════════════════════════════════════════════
    if nd_playlist:
        emit_info(f"[ND] Searching Navidrome for {len(tracks)} discovery track(s) → '{nd_playlist}'")
        nd_found   = []
        nd_missing = []

        for track in tracks:
            if _stop_requested:
                emit_warn("⏹ Stop requested — aborting Navidrome search")
                break
            artist = track.get("artist", "")
            title  = track.get("title",  "")
            emit_info(f"  [ND] Searching: {artist} — {title}")
            sid = importer.search_song(artist, title, conf)
            if sid:
                nd_found.append(sid)
                track["navidrome_id"] = sid
            else:
                nd_missing.append(track)

        emit_info(f"[ND] Found {len(nd_found)}/{len(tracks)} — {len(nd_missing)} not in library")
        _stats["added_nd"] = len(nd_found)
        _stats["missing"]  = len(nd_missing)

        if nd_found and not _stop_requested:
            pid, action = importer.create_or_update_playlist(nd_playlist, nd_found, conf)
            emit_ok(f"[ND] Discovery playlist '{nd_playlist}' {action} — {len(nd_found)} tracks (ID: {pid})")
        elif not nd_found:
            emit_warn("[ND] No discovery tracks found in Navidrome — playlist not updated")

        # ════════════════════════════════════════════
        # PHASE 5 — LIDARR for tracks not in Navidrome
        # Album metadata already resolved in Phase 3.
        # ════════════════════════════════════════════
        if nd_missing and not _stop_requested:
            emit_info("─" * 40)
            emit_info(f"LIDARR PHASE: {len(nd_missing)} discovery track(s) not in library")
            emit_info("─" * 40)
            for t in nd_missing:
                emit_warn(f"  MISSING: {t.get('artist','?')} — {t.get('title','?')} "
                          f"(album: {t.get('album') or 'unknown'})")
            importer.process_missing(nd_missing, conf,
                                     lidarr_mode=group.get("lidarr_mode", "album"))
            emit_info("─" * 40)
            emit_ok("Lidarr phase complete — will appear in Navidrome after download on next sync")

            # Store pending tracks for follow-up check if configured
            fu = group.get("followup", {})
            if fu.get("enabled") and nd_missing:
                from datetime import datetime as _dt, timedelta as _td
                delay_h  = int(fu.get("delay_hours", 24))
                check_at = (_dt.now() + _td(hours=delay_h)).strftime("%Y-%m-%d %H:%M:%S")
                pending  = [{"artist": t.get("artist",""), "title": t.get("title",""),
                             "album": t.get("album","")} for t in nd_missing]
                # Save back to config
                from app import config as _cfg
                _conf = _cfg.load()
                for g in _conf.get("groups", []):
                    if g["id"] == group.get("id"):
                        g.setdefault("followup", {}).update({
                            "pending":       pending,
                            "check_at":      check_at,
                            "attempt":       1,
                            "last_sent_at":  _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
                        })
                        break
                _cfg.save(_conf)
                emit_info(f"[FOLLOWUP] Scheduled follow-up check at {check_at} "
                          f"for {len(pending)} track(s)")
        elif nd_missing and _stop_requested:
            emit_warn("⏹ Stop requested — Lidarr phase skipped")
        else:
            emit_ok("[ND] All discovery tracks found in Navidrome library")

    # Final snapshot update with navidrome_ids filled in
    save_master_snapshot(group, tracks)
    emit_ok(f"Discovery group '{name}' complete")
    return {"name": name, **_stats}


# ═════════════════════════════════════════════════════════════════
#  PER-GROUP SYNC
# ═════════════════════════════════════════════════════════════════

def sync_group(group: dict, conf: dict):
    name = group.get("name", "Unnamed Group")
    emit_info("═" * 50)
    emit_info(f"GROUP: '{name}'")
    emit_info("═" * 50)

    nd_playlist    = group.get("navidrome",   "") if not group.get("_nd_offline") else ""
    sp_playlist_id = group.get("spotify_id",  "")
    yt_playlist_id = group.get("youtube_id",  "")
    m3u_file       = group.get("m3u_file",    "")

    _stats = {"added_sp": 0, "added_yt": 0, "added_nd": 0, "missing": 0, "errors": 0}
    all_track_lists = []
    source_summary  = []

    # ════════════════════════════════════════════════
    # PHASE 1 — FETCH from all sources
    # ════════════════════════════════════════════════

    # Navidrome — discovery only. navidrome_id deliberately dropped:
    # IDs change on re-tag/re-import so we always search fresh on push.
    if nd_playlist:
        emit_info(f"[ND] Fetching Navidrome playlist: '{nd_playlist}'")
        nd_tracks = _get_navidrome_tracks(nd_playlist, conf)
        emit_ok(f"[ND] {len(nd_tracks)} track(s) fetched")
        for t in nd_tracks:
            t.pop("navidrome_id", None)
        tag_track_versions(nd_tracks)
        all_track_lists.append(nd_tracks)
        source_summary.append(f"Navidrome={len(nd_tracks)}")

    if sp_playlist_id and spotify.is_connected(emit=emit_warn):
        emit_info(f"[SP] Fetching Spotify playlist: {sp_playlist_id}")
        try:
            sp_tracks = spotify.get_playlist_tracks(sp_playlist_id)
            emit_ok(f"[SP] {len(sp_tracks)} track(s) fetched")
            tag_track_versions(sp_tracks)
            all_track_lists.append(sp_tracks)
            source_summary.append(f"Spotify={len(sp_tracks)}")
        except Exception as e:
            emit_warn(f"[SP] Fetch failed: {e}")
    elif sp_playlist_id:
        emit_warn("[SP] Playlist configured but Spotify not connected — skipping")

    if yt_playlist_id and youtube.is_connected(emit=emit_warn):
        emit_info(f"[YT] Fetching YouTube Music playlist: {yt_playlist_id}")
        try:
            raw_yt    = youtube.get_playlist_tracks(yt_playlist_id)
            emit_ok(f"[YT] {len(raw_yt)} track(s) fetched")
            yt_tracks = enrich_yt_tracks(raw_yt)  # already tags versions internally
            all_track_lists.append(yt_tracks)
            source_summary.append(f"YouTube={len(yt_tracks)}")
        except Exception as e:
            emit_warn(f"[YT] Fetch failed: {e}")
    elif yt_playlist_id:
        emit_warn("[YT] Playlist configured but YouTube Music not connected — skipping")

    if m3u_file:
        m3u_path = Path("/data/playlists") / m3u_file
        if m3u_path.exists():
            emit_info(f"[M3U] Parsing: '{m3u_file}'")
            m3u_tracks = importer.parse_m3u8(str(m3u_path))
            emit_ok(f"[M3U] {len(m3u_tracks)} track(s) parsed")
            tag_track_versions(m3u_tracks)
            all_track_lists.append(m3u_tracks)
            source_summary.append(f"m3u={len(m3u_tracks)}")
        else:
            emit_warn(f"[M3U] File not found: {m3u_file}")

    if not all_track_lists:
        emit_warn(f"No sources available for group '{name}' — skipping")
        return

    # ════════════════════════════════════════════════
    # PHASE 2 — UNION MERGE into master
    # ════════════════════════════════════════════════
    emit_info(f"Merging {len(all_track_lists)} source(s): {', '.join(source_summary)}")
    master = union_merge(all_track_lists, blacklist=group.get("blacklist", []))
    emit_ok(f"Master playlist: {len(master)} unique track(s) after deduplication")

    # ════════════════════════════════════════════════
    # PHASE 3 — RESTORE cached metadata from previous snapshot
    # album, spotify_id, youtube_id, mb_artist_id, mb_album_id
    # Only fills blanks — never overwrites what the fetch already gave us.
    # ════════════════════════════════════════════════
    _load_cached_fields_from_snapshot(group.get("id", ""), master)

    # ════════════════════════════════════════════════
    # PHASE 4 — ENRICH missing album metadata
    # MB recording search → Spotify search → YTMusic search
    # Runs on every track without an album name so Lidarr has
    # the right album to search for later.
    # ════════════════════════════════════════════════
    needs_album = [t for t in master if not t.get("album", "").strip()]
    if needs_album:
        emit_info(f"Resolving album metadata for {len(needs_album)} track(s) without album info...")
        enriched = enrich_missing_albums(needs_album, conf)
        # Merge resolved albums back onto master (enrich_missing_albums returns
        # the same list objects mutated in place, but be explicit)
        resolved = sum(1 for t in enriched if t.get("album","").strip())
        still_missing = len(enriched) - resolved
        emit_info(f"Album resolution: {resolved} resolved, {still_missing} still unknown")
    else:
        emit_debug("All tracks already have album info — skipping album resolution")

    # Save snapshot now — has full metadata before any pushing happens
    save_master_snapshot(group, master)

    # ════════════════════════════════════════════════
    # PHASE 5 — PUSH to Spotify
    # ════════════════════════════════════════════════
    if sp_playlist_id and spotify.is_connected(emit=emit_warn):
        if _stop_requested:
            emit_warn("⏹ Stop requested — skipping Spotify push")
        else:
            emit_info(f"[SP] Pushing to Spotify playlist {sp_playlist_id}")
            try:
                current_sp  = spotify.get_playlist_tracks(sp_playlist_id)
                current_ids = {t.get("spotify_id") for t in current_sp if t.get("spotify_id")}
                emit_debug(f"[SP] {len(current_ids)} tracks already in Spotify playlist")
                to_add_uris = []
                for track in master:
                    if track.get("spotify_id") and track["spotify_id"] in current_ids:
                        continue
                    if not track.get("spotify_id"):
                        uri = spotify.search_track(track.get("artist",""), track.get("title",""))
                        if uri:
                            track["spotify_id"] = uri.split(":")[-1]
                    if track.get("spotify_id") and track["spotify_id"] not in current_ids:
                        to_add_uris.append(f"spotify:track:{track['spotify_id']}")
                if to_add_uris:
                    spotify.add_tracks_to_playlist(sp_playlist_id, to_add_uris)
                    _stats["added_sp"] += len(to_add_uris)
                    emit_ok(f"[SP] Added {len(to_add_uris)} new track(s)")
                else:
                    emit_ok("[SP] Spotify playlist already up to date")
            except Exception as e:
                _stats["errors"] += 1
                emit_warn(f"[SP] Push failed: {e}")

    # ════════════════════════════════════════════════
    # PHASE 6 — PUSH to YouTube Music
    # ════════════════════════════════════════════════
    if yt_playlist_id and youtube.is_connected(emit=emit_warn):
        if _stop_requested:
            emit_warn("⏹ Stop requested — skipping YouTube push")
        else:
            emit_info(f"[YT] Pushing to YouTube Music playlist {yt_playlist_id}")
            try:
                current_yt   = youtube.get_playlist_tracks(yt_playlist_id)
                current_vids = {t.get("youtube_id") for t in current_yt if t.get("youtube_id")}
                emit_debug(f"[YT] {len(current_vids)} tracks already in YouTube playlist")
                to_add_vids  = []
                for track in master:
                    if track.get("youtube_id") and track["youtube_id"] in current_vids:
                        continue
                    if not track.get("youtube_id"):
                        artist = track.get("artist","")
                        title  = track.get("title","")
                        emit_info(f"  [YT] Searching: {artist} — {title}")
                        vid = youtube.search_track(artist, title, emit_fn=emit_warn)
                        if vid:
                            track["youtube_id"] = vid
                            to_add_vids.append(vid)
                if to_add_vids:
                    youtube.add_tracks_to_playlist(yt_playlist_id, to_add_vids)
                    _stats["added_yt"] += len(to_add_vids)
                    emit_ok(f"[YT] Added {len(to_add_vids)} new track(s)")
                else:
                    emit_ok("[YT] Playlist already up to date")
            except Exception as e:
                _stats["errors"] += 1
                emit_warn(f"[YT] Push failed: {e}")

    # ════════════════════════════════════════════════
    # PHASE 7 — SEARCH Navidrome and build playlist
    # Always searches fresh — never uses cached navidrome_id.
    # ════════════════════════════════════════════════
    if nd_playlist:
        if _stop_requested:
            emit_warn("⏹ Stop requested — skipping Navidrome push")
        else:
            emit_info(f"[ND] Searching Navidrome for {len(master)} track(s) → '{nd_playlist}'")
            nd_found   = []
            nd_missing = []

            for track in master:
                if _stop_requested:
                    emit_warn("⏹ Stop requested — aborting Navidrome search")
                    break
                artist = track.get("artist", "")
                title  = track.get("title",  "")
                emit_info(f"  [ND] Searching: {artist} — {title}")
                sid = importer.search_song(artist, title, conf)
                if sid:
                    nd_found.append(sid)
                    track["navidrome_id"] = sid
                else:
                    nd_missing.append(track)

            emit_info(f"[ND] Found {len(nd_found)}/{len(master)} — {len(nd_missing)} not in library")
            _stats["added_nd"] += len(nd_found)
            _stats["missing"]  += len(nd_missing)

            if nd_found:
                pid, action = importer.create_or_update_playlist(nd_playlist, nd_found, conf)
                emit_ok(f"[ND] Playlist '{nd_playlist}' {action} — {len(nd_found)} tracks (ID: {pid})")
            else:
                emit_warn("[ND] No tracks matched in Navidrome — playlist not updated")

            # ════════════════════════════════════════════
            # PHASE 8 — LIDARR for tracks not in Navidrome
            # Album metadata already resolved in Phase 4.
            # ════════════════════════════════════════════
            if nd_missing and not _stop_requested:
                emit_info("─" * 40)
                emit_info(f"LIDARR PHASE: {len(nd_missing)} track(s) not in Navidrome library")
                emit_info("─" * 40)
                for t in nd_missing:
                    emit_warn(f"  MISSING: {t.get('artist','?')} — {t.get('title','?')} "
                              f"(album: {t.get('album') or 'unknown'})")
                importer.process_missing(nd_missing, conf,
                                         lidarr_mode=group.get("lidarr_mode", "album"))
                emit_info("─" * 40)
                emit_ok("Lidarr phase complete — will appear in Navidrome after download on next sync")
            elif nd_missing and _stop_requested:
                emit_warn("⏹ Stop requested — Lidarr phase skipped")
            else:
                emit_ok("[ND] All tracks found in Navidrome library")

        # Re-save with navidrome_id + any new spotify/youtube IDs resolved during push
        save_master_snapshot(group, master)
        emit_debug("Master snapshot updated")

    emit_ok(f"Group '{name}' complete")
    return {
        "name":     name,
        "added_sp": _stats.get("added_sp", 0),
        "added_yt": _stats.get("added_yt", 0),
        "added_nd": _stats.get("added_nd", 0),
        "missing":  _stats.get("missing",  0),
        "errors":   _stats.get("errors",   0),
    }


# ═════════════════════════════════════════════════════════════════
#  MAIN SYNC ENTRY POINT
# ═════════════════════════════════════════════════════════════════

def run_sync(group_ids: list[str] | None, log_dir: str):
    global _is_running, _stop_requested
    conf   = cfg.load()
    groups = conf.get("groups", [])
    if group_ids:
        groups = [g for g in groups if g.get("id") in group_ids]

    max_logs = conf.get("logs", {}).get("max_per_group", 10)
    verbose  = conf.get("logs", {}).get("verbose", True)

    # ── Determine log directory ───────────────────────────────────
    # Single group  → /data/logs/<GroupName>/sync_<ts>.log
    # Multiple/none → /data/logs/general/sync_<ts>.log
    if len(groups) == 1:
        log_subdir = Path(log_dir) / _safe_name(groups[0].get("name", "group"))
    else:
        log_subdir = Path(log_dir) / "general"
    log_subdir.mkdir(parents=True, exist_ok=True)

    ts_str   = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_path = log_subdir / f"sync_{ts_str}.log"

    original_emit = globals()["emit"]

    # Open log file immediately — write each line as it happens (no buffering)
    try:
        log_fh = open(log_path, "w", encoding="utf-8", buffering=1)  # line-buffered
    except Exception as e:
        print(f"[WARN] Could not open log file {log_path}: {e}")
        log_fh = None

    def emit_and_log(level, msg):
        original_emit(level, msg)
        if log_fh:
            if level == "DEBUG" and not verbose:
                return
            log_fh.write(f"[{datetime.now().strftime('%H:%M:%S')}] [{level}] {msg}\n")

    globals()["emit"] = emit_and_log

    importer._load_mb_cache()
    try:
        emit_info("═" * 50)
        emit_info(f"SYNC STARTED — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        emit_info(f"Groups to process: {len(groups)}")
        emit_info(f"Log: {log_path}")
        emit_info("═" * 50)

        try:
            importer.nd_get("ping", conf)
            emit_ok(f"Navidrome OK — {conf['navidrome']['url']}")
        except Exception as e:
            import traceback
            emit_warn(f"Navidrome unreachable: {e} — ND phases will be skipped, "
                      f"Spotify/YouTube/Lidarr will continue")
            # Patch groups so nd_playlist is blank — disables phases 1,7,8 for ND
            for g in groups:
                g["_nd_offline"] = True

        try:
            status = importer.lidarr_get("system/status", conf)
            emit_ok(f"Lidarr OK — {conf['lidarr']['url']} v{status.get('version','?')}")
        except Exception as e:
            emit_warn(f"Lidarr unavailable: {e} — missing tracks won't be sent to Lidarr")

        sp_ok = spotify.is_connected(emit=emit_warn)
        yt_ok = youtube.is_connected(emit=emit_warn)
        emit_ok(f"Spotify: {'connected' if sp_ok else 'not connected'}")
        emit_ok(f"YouTube Music: {'connected' if yt_ok else 'not connected'}")

        if not groups:
            emit_warn("No groups to sync — configure groups in the Groups tab")
            return

        group_results = []
        for group in groups:
            if _stop_requested:
                emit_warn("⏹ Sync stopped by user request")
                break
            if group.get("type") == "discovery":
                result = sync_discovery_group(group, conf)
            else:
                result = sync_group(group, conf)
            if result:
                group_results.append(result)

        if _stop_requested:
            emit_warn("═" * 50)
            emit_warn(f"SYNC STOPPED — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            emit_warn("═" * 50)
            notify.notify_stopped()
        else:
            emit_info("═" * 50)
            emit_ok(f"SYNC COMPLETE — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            emit_info("═" * 50)
            notify.notify_complete(group_results)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        emit_error(f"Sync failed unexpectedly: {e}\n{tb}")
        notify.notify_error(str(e))
    finally:
        if log_fh:
            try:
                log_fh.close()
            except Exception:
                pass
        globals()["emit"] = original_emit
        _is_running     = False
        _stop_requested = False
        progress_queue.put({"level": "DONE", "msg": "Sync finished",
                            "ts": datetime.now().strftime("%H:%M:%S")})
        try:
            # Prune old logs — keep max_logs per group dir
            old_logs = sorted(log_subdir.glob("sync_*.log"), reverse=True)
            for old in old_logs[max_logs:]:
                old.unlink(missing_ok=True)
        except Exception as e:
            print(f"[ERROR] Could not prune logs: {e}")
        importer._save_mb_cache()
        importer.clear_emit_override()


def _safe_name(s: str) -> str:
    """Convert group name to a safe directory name."""
    import re
    return re.sub(r'[^\w\-]', '_', s).strip('_') or "group"


def start_sync(group_ids: list[str] | None, log_dir: str) -> bool:
    global _is_running, _stop_requested, _sync_thread
    with _run_lock:
        if _is_running:
            return False
        _is_running     = True
        _stop_requested = False
    _stop_event.clear()
    importer.set_stop_flag(False)
    while not progress_queue.empty():
        progress_queue.get_nowait()
    t = threading.Thread(target=run_sync, args=(group_ids, log_dir), daemon=True)
    _sync_thread = t
    t.start()

    # Watchdog — if the sync thread dies without going through finally
    # (e.g. killed externally or OOM), reset state so UI doesn't stay frozen
    def _watchdog():
        t.join()   # wait for sync thread to finish naturally
        global _is_running
        if _is_running:
            print("[WATCHDOG] Sync thread ended but _is_running still True — forcing reset")
            _is_running = False
            progress_queue.put({"level": "ERROR", "msg": "Sync thread ended unexpectedly",
                                "ts": datetime.now().strftime("%H:%M:%S")})
            progress_queue.put({"level": "DONE",  "msg": "Sync finished",
                                "ts": datetime.now().strftime("%H:%M:%S")})

    threading.Thread(target=_watchdog, daemon=True).start()
    return True


def request_stop():
    """
    Request a clean stop.
    Sets _stop_requested and _stop_event so all loop checks and
    interruptible_sleep() calls in importer exit immediately.
    The UI is updated right away; the background thread finishes its
    current cleanup (closing log file, saving MB cache) and exits.
    """
    global _stop_requested, _is_running
    _stop_requested = True
    _stop_event.set()
    importer.set_stop_flag(True)

    # Tell the UI we're done right away
    _is_running = False
    progress_queue.put({"level": "WARN", "msg": "⏹ Sync stopped by user",
                        "ts": datetime.now().strftime("%H:%M:%S")})
    progress_queue.put({"level": "DONE", "msg": "Sync finished",
                        "ts": datetime.now().strftime("%H:%M:%S")})


def is_running() -> bool:
    return _is_running
