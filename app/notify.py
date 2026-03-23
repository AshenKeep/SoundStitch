"""
Notifications — Gotify native + generic webhook.

Fires on:
  - Sync completed  (with per-group summary)
  - Sync failed     (unhandled exception)
  - Sync stopped    (user requested)
  - Token expired   (Spotify / YouTube needs attention)
"""

import json
import threading
import requests
from datetime import datetime
from app import config as cfg


def _conf() -> dict:
    return cfg.load().get("notifications", {})


def _send_gotify(title: str, message: str, priority: int) -> tuple[bool, str]:
    """Send to Gotify. Returns (success, detail_string)."""
    nc  = _conf().get("gotify", {})
    url = nc.get("url", "").rstrip("/")
    tok = nc.get("token", "")
    if not url:
        return False, "no URL configured"
    if not tok:
        return False, "no token configured"
    try:
        resp = requests.post(
            f"{url}/message",
            headers={"X-Gotify-Key": tok},
            json={"title": title, "message": message, "priority": priority},
            timeout=10,
        )
        if resp.ok:
            return True, "sent"
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, str(e)


def _send_webhook(title: str, message: str, priority: int):
    nc  = _conf().get("webhook", {})
    url = nc.get("url", "")
    if not url:
        return

    method   = nc.get("method", "POST").upper()
    raw_hdrs = nc.get("headers", "")
    tmpl     = nc.get("body_template", "")

    headers = {"Content-Type": "application/json"}
    for line in raw_hdrs.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip()] = v.strip()

    if tmpl:
        try:
            body_str = tmpl.replace("{title}", title) \
                           .replace("{message}", message) \
                           .replace("{priority}", str(priority))
            body = json.loads(body_str)
        except Exception:
            body = {"title": title, "message": message, "priority": priority}
    else:
        body = {"title": title, "message": message, "priority": priority}

    try:
        requests.request(method, url, headers=headers, json=body, timeout=10)
    except Exception as e:
        print(f"[NOTIFY] Webhook send failed: {e}")


def _fire(title: str, message: str, priority: int = 5):
    """Send to all enabled channels in a background thread."""
    nc = _conf()

    def _send():
        if nc.get("gotify", {}).get("enabled"):
            ok, detail = _send_gotify(title, message, priority)
            if not ok:
                print(f"[NOTIFY] Gotify failed: {detail}")
        if nc.get("webhook", {}).get("enabled"):
            _send_webhook(title, message, priority)

    threading.Thread(target=_send, daemon=True).start()


# ── Public API ────────────────────────────────────────────────────

def notify_complete(group_results: list[dict]):
    """
    group_results: list of {name, added_sp, added_yt, added_nd, missing, errors}
    """
    nc = _conf()
    if not nc.get("on_complete", True):
        return

    ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"Sync completed at {ts}"]
    total_missing = 0
    total_added   = 0

    for g in group_results:
        name     = g.get("name", "?")
        added    = g.get("added_sp", 0) + g.get("added_yt", 0) + g.get("added_nd", 0)
        missing  = g.get("missing", 0)
        total_added   += added
        total_missing += missing
        parts = []
        if g.get("added_sp"): parts.append(f"+{g['added_sp']} Spotify")
        if g.get("added_yt"): parts.append(f"+{g['added_yt']} YouTube")
        if g.get("added_nd"): parts.append(f"+{g['added_nd']} Navidrome")
        if missing:           parts.append(f"{missing} missing")
        lines.append(f"  {name}: " + (", ".join(parts) if parts else "up to date"))

    if nc.get("on_missing", True) and total_missing:
        lines.append(f"\n{total_missing} track(s) not in library — sent to Lidarr")

    priority = nc.get("gotify", {}).get("priority", 5)
    _fire("SoundStitch — Sync Complete", "\n".join(lines), priority)


def notify_error(error: str):
    nc = _conf()
    if not nc.get("on_error", True):
        return
    priority = max(nc.get("gotify", {}).get("priority", 5), 7)
    _fire("SoundStitch — Sync Failed", f"Sync failed with error:\n\n{error}", priority)


def notify_stopped():
    nc = _conf()
    if not nc.get("on_stopped", False):
        return
    priority = nc.get("gotify", {}).get("priority", 5)
    _fire("SoundStitch — Sync Stopped", "Sync was stopped by user request.", priority)


def notify_token_expired(service: str):
    nc = _conf()
    if not nc.get("on_error", True):
        return
    priority = max(nc.get("gotify", {}).get("priority", 5), 7)
    _fire(
        f"SoundStitch — {service} Disconnected",
        f"{service} token has expired and could not be refreshed.\n"
        f"Please reconnect {service} in Settings.",
        priority,
    )


def test_notify() -> dict:
    """Send a test notification to all enabled channels. Returns status per channel."""
    nc      = _conf()
    results = {}
    ts      = datetime.now().strftime("%H:%M:%S")
    title   = "SoundStitch — Test Notification"
    msg     = f"Test sent at {ts}. If you see this, notifications are working!"

    if nc.get("gotify", {}).get("enabled"):
        ok, detail = _send_gotify(title, msg, 5)
        results["gotify"] = detail  # "sent" or the actual error string
    else:
        results["gotify"] = "disabled"

    if nc.get("webhook", {}).get("enabled"):
        try:
            _send_webhook(title, msg, 5)
            results["webhook"] = "sent"
        except Exception as e:
            results["webhook"] = f"error: {e}"
    else:
        results["webhook"] = "disabled"

    return results
