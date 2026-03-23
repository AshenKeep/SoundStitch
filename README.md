# SoundStitch

A self-hosted playlist sync engine. Union-merges your Navidrome, Spotify, and YouTube Music playlists, pushes missing tracks to Lidarr for download, and keeps everything in sync automatically.

![Version](https://img.shields.io/badge/version-0.6.13-blue)
![Docker](https://img.shields.io/badge/docker-ghcr.io%2Fashenkeep%2Fsoundstitch-blue)

## Features

- **Playlist sync** — merge tracks from Navidrome, Spotify, YouTube Music, and M3U files into a unified master playlist, push back to all platforms
- **Lidarr integration** — missing tracks automatically sent to Lidarr with MusicBrainz metadata for accurate downloads. Configurable per group: grab whole album, track only, or monitor only
- **Discovery playlists** — fresh recommendations from Last.fm (loved tracks, similar artist discovery, genre tags) or any Spotify playlist. Wipes and rebuilds Navidrome playlist on each run with follow-up checks for downloaded tracks
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

That's it — the image is rebuilt automatically whenever a new version is pushed.

## Setup

### Navidrome
Set your Navidrome URL, username, and password in **Connections → Navidrome**.

### Spotify
1. Go to https://developer.spotify.com/dashboard and create an app
2. Add `https://YOUR_HOST_IP:8443/api/spotify/callback` as a Redirect URI
3. Enter your Client ID and Secret in **Connections → Spotify**, then click Connect

### YouTube Music
1. Open `music.youtube.com` in your browser while logged in
2. Press F12 → Network tab → click any request → right-click Request Headers → Copy
3. Paste into **Connections → YouTube Music**

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

## Discovery Playlists

Create discovery playlists in the **Discovery** tab. Sources:
- **Spotify** — any specific playlist (paste the share URL)
- **Last.fm Loved** — tracks you've hearted in Last.fm
- **Last.fm Similar** — top tracks from artists similar to your taste
- **Last.fm Tag** — top tracks for a genre tag (e.g. `trance`, `house`)

Each discovery sync wipes and rebuilds the Navidrome playlist with fresh recommendations. Enable follow-up checks to get notified if Lidarr hasn't downloaded requested tracks after a configurable delay.

## Data

All data lives in `./data/` next to your compose file:
- `data/config.json` — encrypted configuration
- `data/.secret` — encryption key (keep this safe, back it up)
- `data/master/` — playlist snapshots (CSV + M3U8)
- `data/logs/` — sync logs
- `data/backups/` — automatic backups

**Back up your `data/` folder** — it contains your config and playlists.

## Self-Signed Certificate

SoundStitch generates a self-signed certificate on first run. Your browser will show a warning — click Advanced → Proceed. For production use, put a reverse proxy (e.g. Caddy, Nginx Proxy Manager) in front and terminate TLS there.

## License

MIT

## Docker Image Tags

| Tag | Description |
|-----|-------------|
| `latest` | Current stable release — recommended for most users |
| `0.6.13` | Specific version — pin this if you want to control updates manually |
| `dev` | Development build — latest changes, may be unstable |
| `0.6.13-dev` | Specific dev build |

**Stable (default):**
```yaml
image: ghcr.io/ashenkeep/soundstitch:latest
```

**Dev (early access):**
```yaml
image: ghcr.io/ashenkeep/soundstitch:dev
```
