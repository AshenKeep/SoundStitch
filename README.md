# SoundStitch

A self-hosted playlist sync engine. Union-merges your Navidrome, Spotify, and YouTube Music playlists, pushes missing tracks to Lidarr for download, and keeps everything in sync automatically.

![Version](https://img.shields.io/badge/version-0.6.17-blue)
![Docker](https://img.shields.io/badge/docker-ghcr.io%2Fashenkeep%2Fsoundstitch-blue)

## Features

- **Playlist sync** — merge tracks from Navidrome, Spotify, YouTube Music, and M3U files into a unified master playlist, push back to all platforms
- **Lidarr integration** — missing tracks automatically sent to Lidarr with MusicBrainz metadata. Prioritises proper studio album releases over compilations, promos, and samplers. Configurable per group: grab whole album, track only, or monitor only
- **Master playlist management** — view, delete, flag, and manually correct track metadata directly from the UI. Deleted tracks are blacklisted so they never come back on re-sync. Metadata corrections are locked and override future syncs
- **Discovery playlists** — fresh recommendations from Last.fm (loved tracks, similar artist discovery, genre tags) or any Spotify playlist. Wipes and rebuilds Navidrome playlist on each run with configurable follow-up checks for downloaded tracks
- **Schedules** — per-group auto-sync on interval (hourly to monthly) or specific days/times
- **Notifications** — Gotify and webhook support for sync completion, errors, and missing track follow-ups
- **Encrypted storage** — credentials stored encrypted at rest with Fernet symmetric encryption
- **Self-hosted** — runs entirely in Docker, no external services required beyond what you connect

## Quick Start

1. Copy `docker-compose.yml` to a folder on your server
2. Run:
   ```bash
   docker compose up -d
   ```
3. Open `https://YOUR_HOST_IP:8443` in your browser
   - Accept the self-signed certificate warning
4. Log in with `admin` / `changeme` and change your password in Settings

## Updating

```bash
docker compose pull && docker compose restart
```

## Setup

### Navidrome
Set your Navidrome URL, username, and password in **Connections → Navidrome**.

### Spotify

> **Important — OAuth requires HTTPS with a trusted certificate.**
> Spotify's OAuth flow will not complete over a self-signed certificate or plain HTTP. If you are running SoundStitch only on your local network without a reverse proxy, **use the YouTube Music browser headers method instead of OAuth**, or set up a reverse proxy (Caddy, Nginx Proxy Manager, Traefik) with a valid certificate first.
>
> If you are exposing SoundStitch publicly with a real domain and valid cert (e.g. via Let's Encrypt), OAuth works fine.

For setups with a valid certificate:
1. Go to https://developer.spotify.com/dashboard and create an app
2. Add `https://YOUR_DOMAIN:8443/api/spotify/callback` as a Redirect URI
3. Enter your Client ID and Secret in **Connections → Spotify**, then click Connect

For local/self-signed setups:
- Spotify sync groups still work — you can use Spotify playlist IDs directly
- OAuth just cannot complete without a trusted cert, so the Connect button won't work
- Set up a reverse proxy with a real cert to enable full OAuth

### YouTube Music

YouTube Music does **not** use OAuth. Instead it uses browser request headers copied from your logged-in session. This works regardless of whether you have a reverse proxy or public domain.

1. Open `music.youtube.com` in your browser while logged in
2. Press F12 → Network tab → click any request to `music.youtube.com`
3. Right-click the Request Headers section → Copy → Copy request headers
4. Paste into **Connections → YouTube Music → Save Headers & Connect**

> Headers expire after a few weeks. When YouTube Music stops working, re-paste fresh headers.

### Last.fm (for Discovery)
1. Get a free API key at https://www.last.fm/api/account/create
2. Enter your username and API key in **Connections → Last.fm**

### Lidarr
Enter your Lidarr URL and API key in **Settings → Lidarr**.

## Playlist Groups

Create groups in the **Groups** tab. Each group can sync from any combination of:
- A Navidrome playlist
- A Spotify playlist (by ID)
- A YouTube Music playlist (by ID)
- An M3U/M3U8 file

SoundStitch union-merges all sources, pushes new tracks back to each platform, and sends anything missing from your Navidrome library to Lidarr.

## Master Playlist Management

The **Master** tab shows the merged track list saved after each sync. Hover any track to reveal three actions:

- **Edit** — opens a modal to correct the artist, title, and album. Corrections are saved and locked — future syncs will not overwrite them. The track is re-sent to Lidarr with the corrected album on the next sync
- **Flag** — marks the track with a yellow indicator as having bad metadata, without removing it from the playlist. Useful for noting tracks to fix later
- **Delete** — removes the track from the master snapshot and from all linked Navidrome, Spotify, and YouTube playlists. Also adds it to the group's blacklist so it is never re-added on future syncs

## Discovery Playlists

Create discovery playlists in the **Discovery** tab. Sources:
- **Spotify** — any specific playlist (paste the share URL or ID)
- **Last.fm Loved** — tracks you have hearted in Last.fm
- **Last.fm Similar** — top tracks from artists similar to your taste (based on your last 6 months of listening)
- **Last.fm Tag** — top tracks for a genre tag (e.g. `trance`, `house`)

Each discovery sync wipes and rebuilds the Navidrome playlist with fresh recommendations. Enable follow-up checks per discovery group to get notified if Lidarr has not downloaded requested tracks — configurable delay, number of attempts, gap between attempts, and action (notify only, re-send to Lidarr, or re-send silently then notify on failure).

## Track Version Detection

SoundStitch detects version type from track titles on ingest from all sources (Spotify, YouTube Music, Navidrome, M3U):

| Version Type | Detected from |
|---|---|
| `live` | `(Live)`, `(Live at Brixton)`, `(Live from ...)` |
| `acoustic` | `(Acoustic)`, `(Acoustic Version)`, `(MTV Unplugged)` |
| `remix` | `(John 00 Fleming Remix)`, `(Extended Mix)`, `(Club Mix)` |
| `demo` | `(Demo)`, `(Demo Version)` |
| `instrumental` | `(Instrumental)` |
| `remaster` | `(2011 Remaster)`, `(Remastered)` |
| `edit` | `(Radio Edit)`, `(Single Edit)` |
| `cover` | `(Cover)`, `(Tribute)` |
| `studio` | default — no tag |

Version type is used to bias MusicBrainz album lookup — live tracks search for live albums, acoustic tracks search for acoustic releases, remix tracks search for the specific remix single using the remixer's name. **This is especially important for EDM** where the remixer is key context — `Exploration of Space (John 00 Fleming Remix)` will search for a John 00 Fleming remix release, not the original album.

Tracks with different version types are never merged during deduplication — `My Curse` and `My Curse (Acoustic)` are treated as two distinct tracks.

You can correct a wrongly detected version type in the **Master** tab using the Edit action. The Version Type dropdown and Remix Artist field are locked as authoritative metadata.

## Lidarr Album Matching

SoundStitch uses MusicBrainz to resolve album names before sending tracks to Lidarr. The release ranking depends on the track's version type:

- **Studio/default** → proper studio album > deluxe edition > single/EP
- **Live** → live album preferred, studio album as fallback
- **Acoustic** → acoustic/unplugged release preferred
- **Remix** → remix single/EP preferred, uses remixer name in search
- **Demo** → demo/EP preferred
- **Instrumental** → instrumental version preferred

Promos, samplers, and compilation names matching known patterns are always rejected regardless of version type.

If a track ends up with the wrong album, use the **Edit** action in the Master tab to correct and lock the metadata.

## Password Reset

If you are locked out of the UI:

1. Stop the container:
   ```bash
   docker compose down
   ```
2. Add a `RESET_TOKEN` environment variable to your `docker-compose.yml`:
   ```yaml
   environment:
     RESET_TOKEN: "some-secret-word"
   ```
3. Start the container again:
   ```bash
   docker compose up -d
   ```
4. Send a POST request with your new password:
   ```bash
   curl -k -X POST https://YOUR_HOST_IP:8443/api/reset-password \
     -H "Content-Type: application/json" \
     -d '{"token":"some-secret-word","new_password":"yournewpassword"}'
   ```
5. Stop the container, **remove the RESET_TOKEN line**, then start again:
   ```bash
   docker compose down && docker compose up -d
   ```

> Always remove the RESET_TOKEN after use.

## Data

All data lives in `./data/` next to your compose file:
- `data/config.json` — encrypted configuration
- `data/.secret` — encryption key (keep this safe, back it up)
- `data/master/` — playlist snapshots (CSV + M3U8)
- `data/logs/` — sync logs
- `data/backups/` — automatic backups

**Back up your `data/` folder regularly** — it contains your config, encryption key, and playlists. If you lose `data/.secret` your config cannot be decrypted.

## Self-Signed Certificate

SoundStitch generates a self-signed certificate at build time. Your browser will show a warning — click Advanced → Proceed. For production use, put a reverse proxy in front and terminate TLS there. Recommended options:

- **Caddy** — automatic HTTPS with Let's Encrypt, minimal config
- **Nginx Proxy Manager** — GUI-based, good for beginners
- **Traefik** — good if you are already running it for other containers

## Docker Image Tags

| Tag | Description |
|-----|-------------|
| `latest` | Current stable release — recommended for most users |
| `0.6.17` | Specific version — pin this if you want to control updates manually |
| `dev` | Development build — latest changes, may be unstable |
| `0.6.17-dev` | Specific dev build |

**Stable (default):**
```yaml
image: ghcr.io/ashenkeep/soundstitch:latest
```

**Dev (early access):**
```yaml
image: ghcr.io/ashenkeep/soundstitch:dev
```

## License

MIT
