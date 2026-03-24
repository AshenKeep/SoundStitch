import os, json, secrets, zipfile, io, time
from pathlib import Path
from datetime import datetime
import asyncio
from collections import defaultdict

from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
import app.config    as cfg
import app.importer  as importer
import app.sync      as sync
import app.spotify   as spotify
import app.youtube   as youtube
import app.scheduler as scheduler
import app.notify    as notify

PLAYLIST_DIR = os.getenv("PLAYLIST_DIR", "/data/playlists")
LOG_DIR      = os.getenv("LOG_DIR",      "/data/logs")
MASTER_DIR   = "/data/master"
BACKUP_DIR   = "/data/backups"
MAX_UPLOAD_MB = 50   # hard cap on restore/upload endpoints

for d in [PLAYLIST_DIR, LOG_DIR, MASTER_DIR, BACKUP_DIR]:
    Path(d).mkdir(parents=True, exist_ok=True)

# ── Shell / stdout log ────────────────────────────────────────────
# All print() output goes to /data/logs/shell.log in addition to stdout.
# The file is opened once at startup and written to directly by the
# TeeStream wrapper so every line is persisted even if the container crashes.

import sys

class _TeeStream:
    """Write to both the original stdout and a log file."""
    def __init__(self, original, filepath: str):
        self._orig = original
        try:
            self._fh = open(filepath, "a", encoding="utf-8", buffering=1)
        except Exception as e:
            self._fh = None
            print(f"[WARN] Could not open shell log {filepath}: {e}", file=original)

    def write(self, data):
        self._orig.write(data)
        if self._fh:
            try:
                self._fh.write(data)
            except Exception:
                pass

    def flush(self):
        self._orig.flush()
        if self._fh:
            try:
                self._fh.flush()
            except Exception:
                pass

    def fileno(self):
        return self._orig.fileno()

    def isatty(self):
        return False

_shell_log_path = Path(LOG_DIR) / "shell.log"
sys.stdout = _TeeStream(sys.stdout, str(_shell_log_path))
sys.stderr = _TeeStream(sys.stderr, str(_shell_log_path))

# Apply timezone from config (overrides TZ env var if set in UI)
def _apply_timezone():
    try:
        _conf = cfg.load()
        tz = _conf.get("general", {}).get("timezone", "").strip()
        if not tz:
            tz = os.getenv("TZ", "")
        if tz:
            os.environ["TZ"] = tz
            import time as _time
            _time.tzset()
    except Exception:
        pass

_apply_timezone()

app = FastAPI(title="SoundStitch")
_sessions: dict[str, dict] = {}   # token → {username, created_at}

# ── Rate limiter (login brute-force protection) ───────────────────
# Tracks failed attempts per IP. After max_attempts within the window,
# the IP is locked out for lockout_minutes.
_login_attempts: dict[str, list] = defaultdict(list)  # ip → [timestamp, ...]
_login_lockouts: dict[str, float] = {}                 # ip → lockout_until timestamp

def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def _check_rate_limit(ip: str) -> tuple[bool, int]:
    """Returns (is_locked, seconds_remaining). Prunes old attempts."""
    sec  = cfg.load().get("security", {})
    max_attempts  = sec.get("max_login_attempts", 10)
    lockout_secs  = sec.get("lockout_minutes", 60) * 60
    window_secs   = lockout_secs   # use same window as lockout

    now = time.time()
    # Check active lockout
    until = _login_lockouts.get(ip, 0)
    if now < until:
        return True, int(until - now)

    # Prune old attempts outside window
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < window_secs]

    if len(_login_attempts[ip]) >= max_attempts:
        _login_lockouts[ip] = now + lockout_secs
        _login_attempts[ip] = []
        return True, lockout_secs

    return False, 0

def _record_failed_login(ip: str):
    _login_attempts[ip].append(time.time())

def _clear_login_attempts(ip: str):
    _login_attempts.pop(ip, None)
    _login_lockouts.pop(ip, None)


# ── Security headers middleware ───────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"]        = "DENY"
        response.headers["X-XSS-Protection"]       = "1; mode=block"
        response.headers["Referrer-Policy"]         = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"]      = "geolocation=(), microphone=(), camera=()"
        # HSTS — only send over HTTPS (always true for our container)
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Tight CSP — allow self + Google Fonts (used in UI) + inline styles/scripts
        # (inline scripts exist in index.html so we must allow 'unsafe-inline' for now)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        )
        return response

app.add_middleware(SecurityHeadersMiddleware)


# ── Session helpers ───────────────────────────────────────────────
def _session_timeout_hours() -> int:
    return cfg.load().get("security", {}).get("session_timeout_hours", 24)

def verify_session(request: Request) -> bool:
    token = request.cookies.get("session")
    if not token or token not in _sessions:
        return False
    sess = _sessions[token]
    age_hours = (time.time() - sess["created_at"]) / 3600
    if age_hours > _session_timeout_hours():
        del _sessions[token]
        return False
    return True

def require_auth(request: Request):
    if not verify_session(request):
        raise HTTPException(status_code=401)

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse((Path(__file__).parent / "static" / "index.html").read_text())

# Start scheduler
scheduler.start(LOG_DIR)

@app.post("/api/login")
async def login(request: Request):
    ip = _get_client_ip(request)
    locked, secs = _check_rate_limit(ip)
    if locked:
        mins = secs // 60
        raise HTTPException(status_code=429,
            detail=f"Too many failed attempts — locked out for {mins} more minute(s)")
    body = await request.json()
    conf = cfg.load()
    if body.get("username") == conf["auth"]["username"] and \
       body.get("password") == conf["auth"]["password"]:
        _clear_login_attempts(ip)
        token = secrets.token_hex(32)
        _sessions[token] = {"username": body["username"], "created_at": time.time()}
        resp = JSONResponse({"ok": True})
        timeout_secs = _session_timeout_hours() * 3600
        resp.set_cookie("session", token, httponly=True, samesite="strict",
                        max_age=int(timeout_secs))
        return resp
    _record_failed_login(ip)
    locked2, secs2 = _check_rate_limit(ip)
    sec = cfg.load().get("security", {})
    remaining = sec.get("max_login_attempts", 10) - len(_login_attempts.get(ip, []))
    if locked2:
        raise HTTPException(status_code=429,
            detail=f"Too many failed attempts — locked out for {secs2 // 60} minute(s)")
    raise HTTPException(status_code=401,
        detail=f"Invalid credentials ({max(0,remaining)} attempt(s) remaining)")

@app.post("/api/logout")
async def logout(request: Request):
    token = request.cookies.get("session")
    if token in _sessions:
        del _sessions[token]
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("session")
    return resp

@app.post("/api/reset-password")
async def reset_password(request: Request):
    """
    Emergency password reset — no session required.
    Only works if RESET_TOKEN env var is set on the container.
    Usage:  POST /api/reset-password  {"token": "<RESET_TOKEN>", "new_password": "..."}
    Set env var in docker-compose:  RESET_TOKEN=some-secret-value
    Remove it after resetting to disable this endpoint.
    """
    import os
    reset_token = os.environ.get("RESET_TOKEN", "")
    if not reset_token:
        raise HTTPException(status_code=404, detail="Not found")
    body = await request.json()
    if body.get("token") != reset_token:
        raise HTTPException(status_code=403, detail="Invalid token")
    new_pw = body.get("new_password", "").strip()
    if len(new_pw) < 4:
        raise HTTPException(status_code=400, detail="Password too short")
    conf = cfg.load()
    conf["auth"]["password"] = new_pw
    cfg.save(conf)
    _sessions.clear()  # force re-login everywhere
    return {"ok": True, "message": "Password updated — please log in with your new password"}

@app.get("/api/me")
async def me(request: Request):
    if not verify_session(request):
        raise HTTPException(status_code=401)
    return {"ok": True}


# ── Config ────────────────────────────────────────────────────────
MASK = "••••••••"

@app.get("/api/config")
async def get_config(request: Request):
    require_auth(request)
    conf = json.loads(json.dumps(cfg.load()))
    conf["auth"]["password"]             = MASK
    conf["navidrome"]["password"]        = MASK
    conf["spotify"]["client_secret"]     = MASK if conf["spotify"]["client_secret"]    else ""
    conf["youtube"]["client_secret"]     = MASK if conf["youtube"]["client_secret"]    else ""
    conf["musicbrainz"]["client_secret"] = MASK if conf["musicbrainz"]["client_secret"] else ""
    if conf.get("notifications", {}).get("gotify", {}).get("token"):
        conf["notifications"]["gotify"]["token"] = MASK
    return conf

@app.post("/api/config")
async def save_config(request: Request):
    require_auth(request)
    body    = await request.json()
    current = cfg.load()

    def restore(section, field):
        if body.get(section, {}).get(field) == MASK:
            body.setdefault(section, {})[field] = current[section][field]

    restore("auth",        "password")
    restore("navidrome",   "password")
    restore("spotify",     "client_secret")
    restore("youtube",     "client_secret")
    restore("musicbrainz", "client_secret")
    # Restore masked Gotify token
    if body.get("notifications", {}).get("gotify", {}).get("token") == MASK:
        body["notifications"]["gotify"]["token"] = current.get("notifications", {}).get("gotify", {}).get("token", "")
    for platform in ("spotify", "youtube"):
        for key in ("access_token", "refresh_token", "token_expiry"):
            body.setdefault(platform, {})[key] = current[platform].get(key, "")
    body["groups"]   = current.get("groups",   [])
    body["schedule"] = current.get("schedule", {})
    cfg.save(body)
    _apply_timezone()
    return {"ok": True}


# ── Groups ────────────────────────────────────────────────────────
@app.get("/api/groups")
async def get_groups(request: Request):
    require_auth(request)
    return cfg.load().get("groups", [])

@app.post("/api/groups")
async def create_group(request: Request):
    require_auth(request)
    body  = await request.json()
    conf  = cfg.load()
    group = {
        "id":           secrets.token_hex(8),
        "name":         body.get("name",        "New Group"),
        "navidrome":    body.get("navidrome",   ""),
        "spotify_id":   body.get("spotify_id",  ""),
        "youtube_id":   body.get("youtube_id",  ""),
        "m3u_file":     body.get("m3u_file",    ""),
        "lidarr_mode":  body.get("lidarr_mode", "album"),
    }
    conf.setdefault("groups", []).append(group)
    cfg.save(conf)
    return group

@app.put("/api/groups/{group_id}")
async def update_group(group_id: str, request: Request):
    require_auth(request)
    body = await request.json()
    conf = cfg.load()
    for i, g in enumerate(conf.get("groups", [])):
        if g["id"] == group_id:
            conf["groups"][i] = {**g, **{k: v for k, v in body.items() if k != "id"}}
            cfg.save(conf)
            return conf["groups"][i]
    raise HTTPException(status_code=404)

@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: str, request: Request):
    require_auth(request)
    conf = cfg.load()
    conf["groups"] = [g for g in conf.get("groups", []) if g["id"] != group_id]
    cfg.save(conf)
    return {"ok": True}

@app.post("/api/groups/{group_id}/schedule")
async def save_group_schedule(group_id: str, request: Request):
    """Save the per-group schedule (stored on the group itself)."""
    require_auth(request)
    body = await request.json()
    conf = cfg.load()
    for g in conf.get("groups", []):
        if g["id"] == group_id:
            g["schedule"] = {
                "enabled":          bool(body.get("enabled", False)),
                "mode":             body.get("mode", "interval"),
                "interval_minutes": int(body.get("interval_minutes", 360)),
                "days":             body.get("days", list(range(7))),
                "times":            body.get("times", ["06:00"]),
                "last_run":         g.get("schedule", {}).get("last_run"),
                "next_run":         scheduler._calc_next(body) if body.get("enabled") else None,
            }
            cfg.save(conf)
            return g["schedule"]
    raise HTTPException(status_code=404)


# ── Sync ──────────────────────────────────────────────────────────
@app.post("/api/sync/start")
async def start_sync(request: Request):
    require_auth(request)
    body      = await request.json()
    group_ids = body.get("group_ids")
    if sync.is_running():
        return JSONResponse({"ok": False, "reason": "Already running"}, status_code=409)
    return {"ok": sync.start_sync(group_ids, LOG_DIR)}

@app.get("/api/sync/status")
async def sync_status(request: Request):
    require_auth(request)
    return {"running": sync.is_running()}

@app.post("/api/sync/stop")
async def sync_stop(request: Request):
    require_auth(request)
    if not sync.is_running():
        return {"ok": False, "reason": "Not running"}
    sync.request_stop()
    return {"ok": True}

@app.get("/api/sync/stream")
async def sync_stream(request: Request):
    require_auth(request)
    async def generator():
        while True:
            if await request.is_disconnected():
                break
            if not sync.progress_queue.empty():
                item = sync.progress_queue.get_nowait()
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("level") == "DONE":
                    break
            else:
                await asyncio.sleep(0.2)
    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── m3u import ────────────────────────────────────────────────────
@app.post("/api/import/start")
async def start_import(request: Request):
    require_auth(request)
    if importer.is_running():
        return JSONResponse({"ok": False, "reason": "Already running"}, status_code=409)
    return {"ok": importer.start_import(PLAYLIST_DIR, LOG_DIR)}

@app.get("/api/import/status")
async def import_status(request: Request):
    require_auth(request)
    return {"running": importer.is_running()}

@app.get("/api/import/stream")
async def import_stream(request: Request):
    require_auth(request)
    async def generator():
        while True:
            if await request.is_disconnected():
                break
            if not importer.progress_queue.empty():
                item = importer.progress_queue.get_nowait()
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("level") == "DONE":
                    break
            else:
                await asyncio.sleep(0.2)
    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Playlists ─────────────────────────────────────────────────────
@app.get("/api/playlists")
async def list_playlists(request: Request):
    require_auth(request)
    files = []
    for f in sorted(Path(PLAYLIST_DIR).glob("*.m3u*")):
        stat = f.stat()
        files.append({"name": f.name, "size": stat.st_size,
                      "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")})
    return files

@app.post("/api/playlists/upload")
async def upload_playlist(request: Request, files: list[UploadFile] = File(...)):
    require_auth(request)
    saved = []
    for file in files:
        if not file.filename.endswith((".m3u", ".m3u8")):
            continue
        data = await file.read()
        if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"File too large (max {MAX_UPLOAD_MB}MB)")
        (Path(PLAYLIST_DIR) / file.filename).write_bytes(data)
        saved.append(file.filename)
    return {"saved": saved}

@app.delete("/api/playlists/{filename}")
async def delete_playlist(filename: str, request: Request):
    require_auth(request)
    target = Path(PLAYLIST_DIR) / filename
    if target.exists():
        target.unlink()
        return {"ok": True}
    raise HTTPException(status_code=404)


# ── Master snapshots ──────────────────────────────────────────────
def _parse_master_csv(path: Path) -> dict:
    """Read a master CSV file and return metadata + track list."""
    import csv as _csv
    tracks   = []
    saved_at = ""
    name     = ""
    with open(path, newline="", encoding="utf-8") as f:
        content = f.read()

    lines = content.splitlines()
    data_lines = []
    for line in lines:
        # Comment lines are "# Word:" style — the CSV header starts with "#,"
        # (because the first column is literally named "#")
        if line.startswith("# ") or (line.startswith("#") and not line.startswith("#,")):
            # Parse metadata comment
            if "Group:" in line or "Saved:" in line:
                parts = line.lstrip("# ").split("|")
                for p in parts:
                    p = p.strip()
                    if p.startswith("Group:"):
                        name = p[6:].strip()
                    elif p.startswith("Saved:"):
                        saved_at = p[6:].strip()
        elif line.strip():
            data_lines.append(line)

    if data_lines:
        reader = _csv.DictReader(data_lines)
        tracks = list(reader)

    return {"group_name": name or path.stem, "saved_at": saved_at,
            "track_count": len(tracks), "tracks": tracks}


@app.get("/api/master")
async def list_master(request: Request):
    require_auth(request)
    conf   = cfg.load()
    groups = {g["id"]: g["name"] for g in conf.get("groups", [])}
    result = []
    for f in sorted(Path(MASTER_DIR).glob("*.csv")):
        try:
            data = _parse_master_csv(f)
            result.append({
                "group_id":    f.stem,
                "group_name":  data.get("group_name", groups.get(f.stem, f.stem)),
                "saved_at":    data.get("saved_at",   ""),
                "track_count": data.get("track_count", 0),
                "csv_file":    f.name,
                "m3u_file":    f.stem + ".m3u8",
            })
        except Exception:
            pass
    return result

@app.get("/api/master/{group_id}/tracks")
async def get_master_tracks(group_id: str, request: Request):
    require_auth(request)
    target = Path(MASTER_DIR) / f"{group_id}.csv"
    if not target.exists():
        raise HTTPException(status_code=404)
    return _parse_master_csv(target)

@app.get("/api/master/{group_id}/history")
async def get_master_history(group_id: str, request: Request):
    require_auth(request)
    hist_dir = Path(MASTER_DIR) / group_id
    if not hist_dir.exists():
        return []
    result = []
    for f in sorted(hist_dir.glob("*.csv"), reverse=True):
        try:
            data = _parse_master_csv(f)
            result.append({
                "filename":    f.name,
                "saved_at":    data.get("saved_at", ""),
                "track_count": data.get("track_count", 0),
            })
        except Exception:
            pass
    return result

@app.get("/api/master/{group_id}/history/{filename}/tracks")
async def get_history_tracks(group_id: str, filename: str, request: Request):
    require_auth(request)
    target = Path(MASTER_DIR) / group_id / filename
    if not target.exists():
        raise HTTPException(status_code=404)
    return _parse_master_csv(target)

@app.get("/api/master/{group_id}/download/csv")
async def download_master_csv(group_id: str, request: Request):
    require_auth(request)
    target = Path(MASTER_DIR) / f"{group_id}.csv"
    if not target.exists():
        raise HTTPException(status_code=404)
    return StreamingResponse(
        open(target, "rb"),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=master_{group_id}.csv"}
    )

@app.get("/api/master/{group_id}/download/m3u")
async def download_master_m3u(group_id: str, request: Request):
    require_auth(request)
    target = Path(MASTER_DIR) / f"{group_id}.m3u8"
    if not target.exists():
        raise HTTPException(status_code=404)
    return StreamingResponse(
        open(target, "rb"),
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": f"attachment; filename=master_{group_id}.m3u8"}
    )


@app.delete("/api/master/{group_id}/track")
async def delete_master_track(group_id: str, request: Request):
    """
    Remove a track from the master snapshot and from all linked playlists.
    Also adds it to the group's blacklist so it is never re-added on sync.
    Body: {"artist": str, "title": str}
    """
    require_auth(request)
    body   = await request.json()
    artist = body.get("artist", "").strip()
    title  = body.get("title",  "").strip()
    if not artist or not title:
        raise HTTPException(status_code=400, detail="artist and title required")

    conf = cfg.load()

    # Find group
    group = next((g for g in conf.get("groups", []) if g["id"] == group_id), None)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    # 1. Add to group blacklist
    bl = group.setdefault("blacklist", [])
    already = any(
        b.get("artist","").lower() == artist.lower() and
        b.get("title","").lower()  == title.lower()
        for b in bl
    )
    if not already:
        bl.append({"artist": artist, "title": title})
    cfg.save(conf)

    # 2. Remove from master CSV
    csv_path = Path(MASTER_DIR) / f"{group_id}.csv"
    removed_ids = {"navidrome_id": "", "spotify_id": "", "youtube_id": ""}
    if csv_path.exists():
        import csv as _csv
        content    = csv_path.read_text(encoding="utf-8")
        lines      = content.splitlines()
        meta_lines = [l for l in lines if (l.startswith("# ") or
                      (l.startswith("#") and not l.startswith("#,")))]
        data_lines = [l for l in lines if not (l.startswith("# ") or
                      (l.startswith("#") and not l.startswith("#,"))) and l.strip()]

        kept = []
        reader = _csv.DictReader(data_lines)
        for row in reader:
            ra = row.get("artist","").strip().lower()
            rt = row.get("title", "").strip().lower()
            if ra == artist.lower() and rt == title.lower():
                removed_ids["navidrome_id"] = row.get("navidrome_id","").strip()
                removed_ids["spotify_id"]   = row.get("spotify_id",  "").strip()
                removed_ids["youtube_id"]   = row.get("youtube_id",  "").strip()
            else:
                kept.append(row)

        # Re-write CSV with track removed
        fields = reader.fieldnames or ["#","artist","title","album","source",
                                       "navidrome_id","spotify_id","youtube_id",
                                       "mb_artist_id","mb_album_id","flagged","metadata_override"]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            for ml in meta_lines:
                f.write(ml + "\n")
            writer = _csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for i, row in enumerate(kept, 1):
                row["#"] = i
                writer.writerow(row)

    # 3. Remove from Navidrome playlist
    nd_playlist = group.get("navidrome", "") or group.get("discovery_nd_playlist", "")
    nd_errors   = []
    if nd_playlist and removed_ids["navidrome_id"]:
        try:
            existing = importer.get_existing_playlist(nd_playlist, conf)
            if existing:
                data   = importer.nd_get("getPlaylist", conf, {"id": existing["id"]})
                tracks = data.get("playlist", {}).get("entry", [])
                song_ids = [t["id"] for t in tracks if t.get("id") != removed_ids["navidrome_id"]]
                importer.create_or_update_playlist(nd_playlist, song_ids, conf)
        except Exception as e:
            nd_errors.append(str(e))

    # 4. Remove from Spotify playlist
    sp_errors = []
    sp_pid    = group.get("spotify_id", "")
    if sp_pid and removed_ids["spotify_id"]:
        try:
            import app.spotify as _spotify
            if _spotify.is_connected():
                uri = f"spotify:track:{removed_ids['spotify_id']}"
                import requests as _req
                token = _spotify._get_valid_token()
                _req.delete(
                    f"https://api.spotify.com/v1/playlists/{sp_pid}/tracks",
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"},
                    json={"tracks": [{"uri": uri}]},
                    timeout=10
                )
        except Exception as e:
            sp_errors.append(str(e))

    # 5. Remove from YouTube Music playlist
    yt_errors = []
    yt_pid    = group.get("youtube_id", "")
    if yt_pid and removed_ids["youtube_id"]:
        try:
            import app.youtube as _youtube
            if _youtube.is_connected():
                yt = _youtube._get_ytmusic()
                yt.remove_playlist_items(yt_pid,
                    [{"videoId": removed_ids["youtube_id"], "setVideoId": ""}])
        except Exception as e:
            yt_errors.append(str(e))

    return {
        "ok":      True,
        "removed": {"artist": artist, "title": title},
        "ids":     removed_ids,
        "errors":  nd_errors + sp_errors + yt_errors,
    }


@app.post("/api/master/{group_id}/track/flag")
async def flag_master_track(group_id: str, request: Request):
    """
    Toggle the flagged state on a track in the master snapshot.
    Body: {"artist": str, "title": str, "flagged": bool}
    """
    require_auth(request)
    body    = await request.json()
    artist  = body.get("artist", "").strip()
    title   = body.get("title",  "").strip()
    flagged = bool(body.get("flagged", True))
    if not artist or not title:
        raise HTTPException(status_code=400, detail="artist and title required")

    csv_path = Path(MASTER_DIR) / f"{group_id}.csv"
    if not csv_path.exists():
        raise HTTPException(status_code=404)

    import csv as _csv
    content    = csv_path.read_text(encoding="utf-8")
    lines      = content.splitlines()
    meta_lines = [l for l in lines if (l.startswith("# ") or
                  (l.startswith("#") and not l.startswith("#,")))]
    data_lines = [l for l in lines if not (l.startswith("# ") or
                  (l.startswith("#") and not l.startswith("#,"))) and l.strip()]

    rows = list(_csv.DictReader(data_lines))
    fields = _csv.DictReader(data_lines).fieldnames or []
    if "flagged" not in fields:
        fields = list(fields) + ["flagged", "metadata_override"]

    updated = False
    for row in rows:
        if (row.get("artist","").strip().lower() == artist.lower() and
                row.get("title", "").strip().lower() == title.lower()):
            row["flagged"] = "1" if flagged else ""
            updated = True
            break

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        for ml in meta_lines:
            f.write(ml + "\n")
        writer = _csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    return {"ok": updated, "flagged": flagged}


@app.put("/api/master/{group_id}/track/metadata")
async def override_track_metadata(group_id: str, request: Request):
    """
    Save corrected metadata for a track and lock it as authoritative.
    Body: {"artist_orig": str, "title_orig": str,
           "artist": str, "title": str, "album": str}
    Sets metadata_override=True so future syncs don't overwrite corrections.
    """
    require_auth(request)
    body        = await request.json()
    artist_orig = body.get("artist_orig", "").strip()
    title_orig  = body.get("title_orig",  "").strip()
    new_artist  = body.get("artist", "").strip()
    new_title   = body.get("title",  "").strip()
    new_album   = body.get("album",  "").strip()
    if not artist_orig or not title_orig:
        raise HTTPException(status_code=400, detail="artist_orig and title_orig required")

    csv_path = Path(MASTER_DIR) / f"{group_id}.csv"
    if not csv_path.exists():
        raise HTTPException(status_code=404)

    import csv as _csv
    content    = csv_path.read_text(encoding="utf-8")
    lines      = content.splitlines()
    meta_lines = [l for l in lines if (l.startswith("# ") or
                  (l.startswith("#") and not l.startswith("#,")))]
    data_lines = [l for l in lines if not (l.startswith("# ") or
                  (l.startswith("#") and not l.startswith("#,"))) and l.strip()]

    rows   = list(_csv.DictReader(data_lines))
    fields = list(_csv.DictReader(data_lines).fieldnames or [])
    for extra in ("flagged", "metadata_override"):
        if extra not in fields:
            fields.append(extra)

    updated = False
    for row in rows:
        if (row.get("artist","").strip().lower() == artist_orig.lower() and
                row.get("title", "").strip().lower() == title_orig.lower()):
            if new_artist: row["artist"] = new_artist
            if new_title:  row["title"]  = new_title
            if new_album:  row["album"]  = new_album
            row["metadata_override"] = "1"
            row["flagged"]           = ""   # clear flag once corrected
            updated = True
            break

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        for ml in meta_lines:
            f.write(ml + "\n")
        writer = _csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    return {"ok": updated}
@app.get("/api/schedules")
async def get_schedules(request: Request):
    require_auth(request)
    return scheduler.get_schedules()

@app.post("/api/schedules")
async def save_schedules(request: Request):
    require_auth(request)
    body = await request.json()  # list of schedule entries
    if not isinstance(body, list):
        raise HTTPException(status_code=400, detail="Expected a list of schedule entries")
    import secrets as _sec
    for entry in body:
        if not entry.get("id"):
            entry["id"] = _sec.token_hex(6)
        # Recalculate next_run when mode/times/days/interval changed
        entry["next_run"] = scheduler._calc_next(entry) if entry.get("enabled") else entry.get("next_run")
    scheduler.save_schedules(body)
    return body

@app.post("/api/schedules/new")
async def new_schedule(request: Request):
    require_auth(request)
    import secrets as _sec
    entry = scheduler._default_entry()
    entry["id"]    = _sec.token_hex(6)
    entry["label"] = "New Schedule"
    schedules = scheduler.get_schedules()
    schedules.append(entry)
    scheduler.save_schedules(schedules)
    return entry

@app.delete("/api/schedules/{sched_id}")
async def delete_schedule(sched_id: str, request: Request):
    require_auth(request)
    schedules = [s for s in scheduler.get_schedules() if s.get("id") != sched_id]
    scheduler.save_schedules(schedules)
    return {"ok": True}

@app.post("/api/schedule/run-now")
async def schedule_run_now(request: Request):
    require_auth(request)
    if sync.is_running():
        return JSONResponse({"ok": False, "reason": "Already running"}, status_code=409)
    # Run all enabled groups from first enabled schedule, or all groups
    schedules = scheduler.get_schedules()
    enabled   = [s for s in schedules if s.get("enabled")]
    group_ids = enabled[0].get("group_ids") or None if enabled else None
    ok        = sync.start_sync(group_ids, LOG_DIR)
    return {"ok": ok}

# Legacy single-schedule endpoints (kept for backward compat)
@app.get("/api/schedule")
async def get_schedule_legacy(request: Request):
    require_auth(request)
    schedules = scheduler.get_schedules()
    return schedules[0] if schedules else scheduler._default_entry()

@app.post("/api/schedule")
async def save_schedule_legacy(request: Request):
    require_auth(request)
    body      = await request.json()
    schedules = scheduler.get_schedules()
    if schedules:
        schedules[0].update(body)
        schedules[0]["next_run"] = scheduler._calc_next(schedules[0])
    else:
        schedules = [{**scheduler._default_entry(), **body}]
    scheduler.save_schedules(schedules)
    return schedules[0]


# ── Backup ────────────────────────────────────────────────────────
@app.get("/api/backups")
async def list_backups(request: Request):
    require_auth(request)
    backups = []
    for f in sorted(Path(BACKUP_DIR).glob("*.zip"), reverse=True):
        stat = f.stat()
        backups.append({"name": f.name, "size": stat.st_size,
                        "created": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
    return backups

@app.post("/api/backups/create")
async def create_backup(request: Request):
    require_auth(request)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"backup_{ts}.zip"
    out_path = Path(BACKUP_DIR) / filename

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Encryption key — must be backed up so encrypted credentials can be restored
        secret_path = Path("/data/.secret")
        if secret_path.exists():
            zf.write(secret_path, ".secret")

        # config.json (credentials are encrypted with the above key)
        config_path = Path("/data/config.json")
        if config_path.exists():
            zf.write(config_path, "config.json")

        # All master playlist files — CSV, M3U8, and history subdirs
        master_root = Path(MASTER_DIR)
        for f in master_root.rglob("*"):
            if f.is_file():
                rel = f.relative_to(master_root)
                zf.write(f, f"master/{rel}")

        # uploaded m3u files
        for f in Path(PLAYLIST_DIR).glob("*.m3u*"):
            zf.write(f, f"playlists/{f.name}")

        # recent logs — all files in all subdirs, up to 30 total
        all_log_files = sorted(Path(LOG_DIR).rglob("*.log"), reverse=True)[:30]
        for f in all_log_files:
            rel = f.relative_to(Path(LOG_DIR))
            zf.write(f, f"logs/{rel}")

    stat = out_path.stat()
    # Keep only 10 backups
    all_backups = sorted(Path(BACKUP_DIR).glob("*.zip"), reverse=True)
    for old in all_backups[10:]:
        old.unlink()

    return {"name": filename, "size": stat.st_size}

@app.get("/api/backups/{filename}")
async def download_backup(filename: str, request: Request):
    require_auth(request)
    target = Path(BACKUP_DIR) / filename
    if not target.exists():
        raise HTTPException(status_code=404)
    return StreamingResponse(
        open(target, "rb"),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.delete("/api/backups/{filename}")
async def delete_backup(filename: str, request: Request):
    require_auth(request)
    target = Path(BACKUP_DIR) / filename
    if target.exists():
        target.unlink()
        return {"ok": True}
    raise HTTPException(status_code=404)

@app.get("/api/backup-schedule")
async def get_backup_schedule(request: Request):
    require_auth(request)
    return cfg.load().get("backup_schedule", cfg.DEFAULTS["backup_schedule"])

@app.post("/api/backup-schedule")
async def save_backup_schedule(request: Request):
    require_auth(request)
    body = await request.json()
    conf = cfg.load()
    conf["backup_schedule"] = {
        "enabled":        bool(body.get("enabled", False)),
        "mode":           body.get("mode", "interval"),
        "interval_hours": int(body.get("interval_hours", 24)),
        "days":           body.get("days", list(range(7))),
        "time":           body.get("time", "02:00"),
        "last_backup":    conf.get("backup_schedule", {}).get("last_backup"),
        "next_run":       conf.get("backup_schedule", {}).get("next_run"),
    }
    cfg.save(conf)
    return conf["backup_schedule"]

@app.post("/api/backups/restore")
async def restore_backup(request: Request, file: UploadFile = File(...)):
    require_auth(request)
    if not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Must be a .zip backup file")
    data = await file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_UPLOAD_MB}MB)")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names    = zf.namelist()
            restored = []

            # ── Step 1: restore the encryption key first ──────────────
            # This must happen before loading config.json so the Fernet
            # instance is built with the backup's key, allowing all
            # encrypted credentials to decrypt correctly.
            if ".secret" in names:
                secret_path = Path("/data/.secret")
                secret_path.write_bytes(zf.read(".secret"))
                try:
                    secret_path.chmod(0o600)
                except Exception:
                    pass
                # Invalidate the cached Fernet instance so it rebuilds
                # with the just-restored key on the next cfg.load() call
                cfg._fernet_instance = None
                restored.append(".secret")

            # ── Step 2: restore config (now decryptable with above key) ─
            if "config.json" in names:
                try:
                    restored_conf = json.loads(zf.read("config.json"))
                    # Pass through cfg.save — values are already encrypted
                    # with the restored key; save re-encrypts them cleanly
                    # (handles legacy plain-text configs transparently too)
                    cfg.save(cfg.deep_merge(cfg.DEFAULTS.copy(), restored_conf))
                except Exception as e:
                    raise HTTPException(status_code=400, detail=f"Config restore failed: {e}")
                restored.append("config.json")

            # ── Step 3: restore master playlists ──────────────────────
            for name in names:
                if name.startswith("master/") and not name.endswith("/"):
                    rel = name[len("master/"):]
                    if not rel:
                        continue
                    target = Path(MASTER_DIR) / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(name))
                    restored.append(name)

                elif name.startswith("playlists/") and name.endswith((".m3u", ".m3u8")):
                    fname  = Path(name).name
                    target = Path(PLAYLIST_DIR) / fname
                    target.write_bytes(zf.read(name))
                    restored.append(name)

        return {"ok": True, "restored": restored}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Restore failed: {e}")


# ── Spotify ───────────────────────────────────────────────────────
@app.get("/api/spotify/auth-url")
async def spotify_auth_url(request: Request):
    require_auth(request)
    return {"url": spotify.get_auth_url()}

@app.get("/api/spotify/callback")
async def spotify_callback(code: str, state: str, request: Request):
    ok     = spotify.handle_callback(code, state)
    status = "connected" if ok else "error"
    return HTMLResponse(f"""<html><body><script>
        window.opener && window.opener.postMessage({{spotify:'{status}'}}, '*');
        window.close();
    </script><p>Spotify {status}. You can close this window.</p></body></html>""")

@app.get("/api/spotify/playlists")
async def get_spotify_playlists(request: Request):
    require_auth(request)
    try:
        return spotify.get_playlists()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Spotify error: {e}")

@app.post("/api/spotify/disconnect")
async def spotify_disconnect(request: Request):
    require_auth(request)
    conf = cfg.load()
    conf["spotify"]["access_token"] = ""
    conf["spotify"]["refresh_token"] = ""
    conf["spotify"]["token_expiry"]  = 0
    conf["spotify"]["enabled"]       = False
    cfg.save(conf)
    return {"ok": True}


# ── YouTube ───────────────────────────────────────────────────────
@app.get("/api/youtube/auth-url")
async def youtube_auth_url(request: Request):
    require_auth(request)
    return {"url": youtube.get_auth_url()}

@app.get("/api/youtube/callback")
async def youtube_callback(code: str, state: str, request: Request):
    ok     = youtube.handle_callback(code, state)
    status = "connected" if ok else "error"
    return HTMLResponse(f"""<html><body><script>
        window.opener && window.opener.postMessage({{youtube:'{status}'}}, '*');
        window.close();
    </script><p>YouTube {status}. You can close this window.</p></body></html>""")

@app.post("/api/youtube/headers")
async def youtube_save_headers(request: Request):
    require_auth(request)
    body = await request.json()
    raw  = body.get("headers_raw", "").strip()
    if not raw:
        return JSONResponse({"ok": False, "reason": "No headers provided"}, status_code=400)

    # Convert raw browser headers → ytmusicapi JSON format immediately.
    # This way the stored value is always the converted form and we catch
    # bad pastes at save time rather than at sync time.
    try:
        auth_json = youtube._headers_raw_to_ytmusic_auth(raw)
    except Exception as e:
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=400)

    conf = cfg.load()
    conf["youtube"]["headers_raw"] = auth_json   # store converted JSON
    conf["youtube"]["method"]      = "headers"
    conf["youtube"]["enabled"]     = True
    cfg.save(conf)
    youtube._invalidate_ytmusic_cache()   # force rebuild with new headers
    return {"ok": True}

@app.get("/api/youtube/check")
async def check_youtube(request: Request):
    """Test current YouTube Music connection and return detailed status."""
    require_auth(request)
    conf   = cfg.load()
    method = conf["youtube"].get("method", "headers")
    if not conf["youtube"].get("enabled"):
        return {"ok": False, "reason": "YouTube Music not enabled"}
    if method == "headers":
        if not conf["youtube"].get("headers_raw"):
            return {"ok": False, "reason": "No headers stored — paste fresh headers in Settings"}
        try:
            yt = youtube._get_ytmusic()
            yt.get_library_playlists(limit=1)
            return {"ok": True, "method": "headers"}
        except Exception as e:
            youtube._invalidate_ytmusic_cache()
            return {"ok": False, "reason": f"Headers appear expired: {e}. Re-paste fresh headers in Settings."}
    else:
        if not conf["youtube"].get("access_token"):
            return {"ok": False, "reason": "No OAuth token — reconnect via Settings"}
        return {"ok": True, "method": "oauth"}


@app.get("/api/spotify/check")
async def check_spotify(request: Request):
    """Test current Spotify connection and return detailed status."""
    require_auth(request)
    conf = cfg.load()
    if not conf["spotify"].get("enabled"):
        return {"ok": False, "reason": "Spotify not enabled"}
    if not conf["spotify"].get("refresh_token") and not conf["spotify"].get("access_token"):
        return {"ok": False, "reason": "No tokens — reconnect Spotify in Settings"}
    try:
        token = spotify._get_valid_token()
        return {"ok": True, "token_expires_in": int(conf["spotify"].get("token_expiry", 0) - __import__("time").time())}
    except RuntimeError as e:
        return {"ok": False, "reason": str(e)}

@app.get("/api/youtube/playlists")
async def get_youtube_playlists(request: Request):
    require_auth(request)
    try:
        return youtube.get_playlists()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"YouTube Music error: {e}")

@app.post("/api/youtube/disconnect")
async def youtube_disconnect(request: Request):
    require_auth(request)
    conf = cfg.load()
    conf["youtube"]["access_token"]  = ""
    conf["youtube"]["refresh_token"] = ""
    conf["youtube"]["token_expiry"]  = 0
    conf["youtube"]["headers_raw"]   = ""
    cfg.save(conf)
    return {"ok": True}

@app.get("/api/navidrome/playlists")
async def get_navidrome_playlists(request: Request):
    require_auth(request)
    conf = cfg.load()
    try:
        data = importer.nd_get("getPlaylists", conf)
        pls  = data.get("playlists", {}).get("playlist", [])
        return [{"id": p["id"], "name": p["name"], "tracks": p.get("songCount", 0)} for p in pls]
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Logs ──────────────────────────────────────────────────────────
@app.get("/api/logs")
async def list_logs(request: Request):
    require_auth(request)
    logs = []
    log_root = Path(LOG_DIR)

    # shell.log — persistent stdout/stderr capture
    shell_log = log_root / "shell.log"
    if shell_log.exists():
        stat = shell_log.stat()
        logs.append({
            "name":     "shell.log",
            "path":     "shell.log",
            "group":    "system",
            "size":     stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })

    # Flat .log files at root level (legacy / other)
    for f in sorted(log_root.glob("*.log"), reverse=True):
        if f.name == "shell.log":
            continue   # already added above
        stat = f.stat()
        logs.append({
            "name":     f.name,
            "path":     f.name,
            "group":    "general",
            "size":     stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    # Per-group subdirectories
    for subdir in sorted(log_root.iterdir()):
        if not subdir.is_dir():
            continue
        for f in sorted(subdir.glob("*.log"), reverse=True):
            stat = f.stat()
            logs.append({
                "name":     f.name,
                "path":     f"{subdir.name}/{f.name}",
                "group":    subdir.name,
                "size":     stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
    # Sort: system first, then by modified desc
    logs.sort(key=lambda x: (0 if x["group"] == "system" else 1, x["modified"]), reverse=True)
    # But keep system at top regardless of date
    system = [l for l in logs if l["group"] == "system"]
    rest   = sorted([l for l in logs if l["group"] != "system"],
                    key=lambda x: x["modified"], reverse=True)
    return system + rest

@app.get("/api/logs/{log_path:path}")
async def get_log_file(log_path: str, request: Request, download: bool = False):
    require_auth(request)
    target = Path(LOG_DIR) / log_path
    try:
        target.resolve().relative_to(Path(LOG_DIR).resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Log not found")
    if download:
        return StreamingResponse(
            open(target, "rb"),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={target.name}"}
        )
    return {"content": target.read_text(encoding="utf-8", errors="replace")}



# ── Notifications ─────────────────────────────────────────────────

@app.get("/api/notifications")
async def get_notifications(request: Request):
    require_auth(request)
    conf = cfg.load()
    n = conf.get("notifications", cfg.DEFAULTS["notifications"])
    # Deep copy and mask token for transport — never send the real token to the UI
    import copy
    n = copy.deepcopy(n)
    if n.get("gotify", {}).get("token"):
        n["gotify"]["token"] = MASK
    return n

@app.post("/api/notifications")
async def save_notifications(request: Request):
    require_auth(request)
    body = await request.json()
    conf = cfg.load()
    existing = conf.get("notifications", cfg.DEFAULTS["notifications"])
    # Restore masked token
    if body.get("gotify", {}).get("token") == MASK or not body.get("gotify", {}).get("token"):
        body.setdefault("gotify", {})["token"] = existing.get("gotify", {}).get("token", "")
    conf["notifications"] = body
    cfg.save(conf)
    return {"ok": True}

@app.post("/api/notifications/test")
async def test_notifications(request: Request):
    require_auth(request)
    results = notify.test_notify()
    return {"ok": True, "results": results}

@app.post("/api/notifications/test-gotify")
async def test_gotify_direct(request: Request):
    """
    Test Gotify with URL+token supplied directly in the request body.
    Does NOT read from config — avoids the mask/restore problem entirely.
    """
    require_auth(request)
    body     = await request.json()
    url      = body.get("url", "").rstrip("/")
    token    = body.get("token", "")
    priority = int(body.get("priority", 5))
    if not url or not token:
        raise HTTPException(status_code=400, detail="url and token are required")
    import requests as _req
    from datetime import datetime as _dt
    try:
        resp = _req.post(
            f"{url}/message",
            headers={"X-Gotify-Key": token},
            json={
                "title":    "SoundStitch — Test Notification",
                "message":  f"Test sent at {_dt.now().strftime('%H:%M:%S')}. Gotify is working!",
                "priority": priority,
            },
            timeout=10,
        )
        if resp.ok:
            return {"ok": True}
        return JSONResponse(
            {"ok": False, "detail": f"HTTP {resp.status_code}: {resp.text[:200]}"},
            status_code=200   # return 200 so client can read the body
        )
    except Exception as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=200)


# ── Discovery ─────────────────────────────────────────────────────

@app.get("/api/discovery/groups")
async def list_discovery_groups(request: Request):
    require_auth(request)
    conf = cfg.load()
    return [g for g in conf.get("groups", []) if g.get("type") == "discovery"]

@app.post("/api/discovery/groups")
async def create_discovery_group(request: Request):
    require_auth(request)
    body  = await request.json()
    conf  = cfg.load()
    group = {
        "id":                    secrets.token_hex(8),
        "type":                  "discovery",
        "name":                  body.get("name", "Discovery"),
        "discovery_source":      body.get("discovery_source", "spotify_playlist"),
        "discovery_nd_playlist": body.get("discovery_nd_playlist", ""),
        "discovery_limit":       int(body.get("discovery_limit", 30)),
        "lastfm_username":       body.get("lastfm_username", ""),
        "lastfm_tag":            body.get("lastfm_tag", ""),
        "spotify_playlist_id":   body.get("spotify_playlist_id", ""),
        "lidarr_mode":           body.get("lidarr_mode", "track"),
        "followup":              body.get("followup", {"enabled": False}),
    }
    conf.setdefault("groups", []).append(group)
    cfg.save(conf)
    return group

@app.put("/api/discovery/groups/{group_id}")
async def update_discovery_group(group_id: str, request: Request):
    require_auth(request)
    body = await request.json()
    conf = cfg.load()
    for i, g in enumerate(conf.get("groups", [])):
        if g["id"] == group_id and g.get("type") == "discovery":
            conf["groups"][i] = {**g, **{k: v for k, v in body.items() if k != "id"}}
            cfg.save(conf)
            return conf["groups"][i]
    raise HTTPException(status_code=404)

@app.delete("/api/discovery/groups/{group_id}")
async def delete_discovery_group(group_id: str, request: Request):
    require_auth(request)
    conf = cfg.load()
    conf["groups"] = [g for g in conf.get("groups", []) if g["id"] != group_id]
    cfg.save(conf)
    return {"ok": True}

@app.post("/api/discovery/sync/{group_id}")
async def run_discovery_sync(group_id: str, request: Request):
    require_auth(request)
    if sync.is_running():
        return JSONResponse({"ok": False, "reason": "Already running"}, status_code=409)
    return {"ok": sync.start_sync([group_id], LOG_DIR)}

@app.get("/api/lastfm/config")
async def get_lastfm_config(request: Request):
    require_auth(request)
    conf = cfg.load()
    lf   = conf.get("lastfm", cfg.DEFAULTS["lastfm"])
    import copy; lf = copy.deepcopy(lf)
    if lf.get("api_secret"):  lf["api_secret"]  = MASK
    if lf.get("session_key"): lf["session_key"]  = MASK
    return lf

@app.post("/api/lastfm/config")
async def save_lastfm_config(request: Request):
    require_auth(request)
    body = await request.json()
    conf = cfg.load()
    existing = conf.get("lastfm", {})
    if body.get("api_secret")  == MASK: body["api_secret"]  = existing.get("api_secret",  "")
    if body.get("session_key") == MASK: body["session_key"] = existing.get("session_key", "")
    conf["lastfm"] = body
    cfg.save(conf)
    return {"ok": True}

@app.post("/api/lastfm/test")
async def test_lastfm_connection(request: Request):
    """Test Last.fm credentials by calling user.getInfo."""
    require_auth(request)
    body    = await request.json()
    api_key = body.get("api_key", "").strip()
    username = body.get("username", "").strip()
    if not api_key or not username:
        raise HTTPException(status_code=400, detail="api_key and username required")
    import requests as _req
    try:
        resp = _req.get("https://ws.audioscrobbler.com/2.0/", params={
            "method":  "user.getInfo",
            "user":    username,
            "api_key": api_key,
            "format":  "json",
        }, timeout=10)
        data = resp.json()
        if "error" in data:
            return {"ok": False, "error": data.get("message", f"Error {data['error']}")}
        user_data    = data.get("user", {})
        display_name = user_data.get("realname") or user_data.get("name") or username
        return {"ok": True, "display_name": display_name}
    except Exception as e:
        return {"ok": False, "error": str(e)}
