"""
Scheduler — supports two modes per schedule entry:

  interval mode:
    { "mode": "interval", "interval_minutes": 360 }
    Fires every N minutes after the last run.

  timed mode:
    { "mode": "timed", "days": [0,1,2,3,4,5,6], "times": ["06:00","18:00"] }
    Fires at specific times on specific weekdays (0=Mon … 6=Sun).

Config lives in config.json under "schedules" (list) for per-group entries,
plus a legacy "schedule" key kept for backward compat.

Each entry:
  {
    "id":               "abc123",
    "enabled":          true,
    "label":            "Daily trance",
    "group_ids":        ["grpid1"],   # null/[] = all groups
    "mode":             "interval",   # or "timed"
    "interval_minutes": 360,
    "days":             [0,1,2,3,4],  # Mon–Fri (timed mode)
    "times":            ["06:00","22:00"],  # (timed mode)
    "last_run":         "2026-03-17 06:00:00",
    "next_run":         "2026-03-17 22:00:00",
  }
"""

import threading
import time
import secrets
from datetime import datetime, timedelta
from app import config as cfg

_thread     = None
_stop_event = threading.Event()

FMT = "%Y-%m-%d %H:%M:%S"


def _fmt(dt: datetime) -> str:
    return dt.strftime(FMT)


def _default_entry() -> dict:
    """A single default schedule entry (backward compat with old single-schedule UI)."""
    return {
        "id":               "default",
        "enabled":          False,
        "label":            "Default",
        "group_ids":        None,
        "mode":             "interval",
        "interval_minutes": 360,
        "days":             [0, 1, 2, 3, 4, 5, 6],
        "times":            ["06:00"],
        "last_run":         None,
        "next_run":         None,
    }


def get_schedules() -> list[dict]:
    conf = cfg.load()
    # Migrate legacy single "schedule" key → list
    if "schedules" in conf:
        return conf["schedules"]
    legacy = conf.get("schedule", {})
    if legacy:
        entry = _default_entry()
        entry["enabled"]          = legacy.get("enabled", False)
        entry["interval_minutes"] = legacy.get("interval_minutes", 360)
        entry["group_ids"]        = legacy.get("group_ids") or None
        entry["last_run"]         = legacy.get("last_run")
        entry["next_run"]         = legacy.get("next_run")
        return [entry]
    return [_default_entry()]


def save_schedules(schedules: list[dict]):
    conf = cfg.load()
    conf["schedules"] = schedules
    # Keep legacy key in sync for anything that reads it directly
    if schedules:
        s = schedules[0]
        conf["schedule"] = {
            "enabled":          s.get("enabled", False),
            "interval_minutes": s.get("interval_minutes", 360),
            "group_ids":        s.get("group_ids"),
            "last_run":         s.get("last_run"),
            "next_run":         s.get("next_run"),
        }
    cfg.save(conf)


def _next_interval(entry: dict) -> datetime:
    mins = int(entry.get("interval_minutes", 360))
    return datetime.now() + timedelta(minutes=mins)


def _next_timed(entry: dict, after: datetime = None) -> datetime | None:
    """Return the next datetime matching days+times after `after` (default now)."""
    after = after or datetime.now()
    days  = entry.get("days", list(range(7)))
    times = sorted(entry.get("times", ["06:00"]))
    if not days or not times:
        return None

    # Search up to 8 days ahead
    for day_offset in range(8):
        candidate_date = (after + timedelta(days=day_offset)).date()
        if candidate_date.weekday() not in days:
            continue
        for t_str in times:
            try:
                h, m = map(int, t_str.split(":"))
            except ValueError:
                continue
            candidate = datetime(candidate_date.year, candidate_date.month,
                                 candidate_date.day, h, m)
            if candidate > after:
                return candidate
    return None


def _calc_next(entry: dict) -> str | None:
    mode = entry.get("mode", "interval")
    if mode == "timed":
        dt = _next_timed(entry)
        return _fmt(dt) if dt else None
    else:
        return _fmt(_next_interval(entry))


def _is_due(entry: dict) -> bool:
    next_str = entry.get("next_run")
    if not next_str:
        return False
    try:
        return datetime.now() >= datetime.strptime(next_str, FMT)
    except ValueError:
        return False


def record_run(entry: dict) -> dict:
    entry["last_run"] = _fmt(datetime.now())
    entry["next_run"] = _calc_next(entry)
    return entry


def _loop(log_dir: str):
    import app.sync as sync
    print("[SCHEDULER] Thread started")

    while not _stop_event.is_set():
        try:
            # ── Global schedules (schedule tab) ───────────────────
            schedules = get_schedules()
            updated   = False

            for entry in schedules:
                if not entry.get("enabled"):
                    continue
                if not entry.get("next_run"):
                    entry["next_run"] = _calc_next(entry)
                    updated = True
                    continue
                if not _is_due(entry):
                    continue
                if sync.is_running():
                    print(f"[SCHEDULER] '{entry.get('label','?')}' due but sync running — rescheduling")
                    entry["next_run"] = _calc_next(entry)
                    updated = True
                    continue
                group_ids = entry.get("group_ids") or None
                print(f"[SCHEDULER] Firing '{entry.get('label','?')}' groups={group_ids}")
                sync.start_sync(group_ids, log_dir)
                entry = record_run(entry)
                print(f"[SCHEDULER] Next: {entry['next_run']}")
                updated = True

            if updated:
                save_schedules(schedules)

            # ── Per-group schedules (stored on each group) ────────
            _check_group_schedules(sync, log_dir)

            # ── Discovery follow-up checks ────────────────────────
            _check_discovery_followups()

            # ── Backup schedule ───────────────────────────────────
            _check_backup_schedule()

        except Exception as e:
            print(f"[SCHEDULER] Error in loop: {e}")

        _stop_event.wait(30)

    print("[SCHEDULER] Thread stopped")


def _check_group_schedules(sync, log_dir: str):
    """
    Check each group's own schedule (stored as group['schedule']).
    Schema identical to global schedule entries but stored on the group.
    """
    try:
        from app import config as _cfg
        conf    = _cfg.load()
        groups  = conf.get("groups", [])
        changed = False

        for g in groups:
            sched = g.get("schedule")
            if not sched or not sched.get("enabled"):
                continue
            if not sched.get("next_run"):
                sched["next_run"] = _calc_next(sched)
                changed = True
                continue
            if not _is_due(sched):
                continue
            if sync.is_running():
                sched["next_run"] = _calc_next(sched)
                changed = True
                continue
            print(f"[SCHEDULER] Per-group: firing '{g.get('name','?')}'")
            sync.start_sync([g["id"]], log_dir)
            sched["last_run"] = _fmt(datetime.now())
            sched["next_run"] = _calc_next(sched)
            changed = True

        if changed:
            _cfg.save(conf)
    except Exception as e:
        print(f"[SCHEDULER] Per-group schedule error: {e}")


def _check_discovery_followups():
    """
    Check pending follow-up tracks for discovery groups.
    For each discovery group with followup.enabled and followup.pending:
      - If check_at time has passed, re-search Navidrome for each pending track
      - If still missing: act per followup.action (notify / resend+notify / resend silent)
      - If found: remove from pending
      - Reschedule or clear based on max_attempts
    """
    try:
        from app import config as _cfg
        import app.importer as _imp
        import app.notify as _notify
        conf    = _cfg.load()
        changed = False
        now     = datetime.now()

        for g in conf.get("groups", []):
            if g.get("type") != "discovery":
                continue
            fu = g.get("followup", {})
            if not fu.get("enabled") or not fu.get("pending"):
                continue
            check_at_str = fu.get("check_at")
            if not check_at_str:
                continue
            try:
                check_at = datetime.strptime(check_at_str, FMT)
            except ValueError:
                continue
            if now < check_at:
                continue

            # Due — run follow-up check
            pending   = fu.get("pending", [])
            attempt   = fu.get("attempt", 1)
            max_att   = int(fu.get("max_attempts", 2))
            gap_h     = int(fu.get("gap_hours", 24))
            action    = fu.get("action", "resend_notify")
            threshold = int(fu.get("threshold", 1))
            print(f"[SCHEDULER] Discovery follow-up: '{g.get('name','?')}' "
                  f"attempt {attempt}, checking {len(pending)} track(s)")

            still_missing = []
            for t in pending:
                artist = t.get("artist", "")
                title  = t.get("title",  "")
                sid    = _imp.search_song(artist, title, conf)
                if not sid:
                    still_missing.append(t)

            found_count = len(pending) - len(still_missing)
            print(f"[SCHEDULER] Follow-up: {found_count} found, {len(still_missing)} still missing")

            if still_missing and len(still_missing) >= threshold:
                # Act on still-missing tracks
                msg_lines = [
                    f"Discovery follow-up — '{g.get('name','?')}' (attempt {attempt})",
                    f"{len(still_missing)} track(s) still not in Navidrome library:",
                ]
                for t in still_missing[:10]:
                    msg_lines.append(f"  - {t.get('artist','?')} — {t.get('title','?')}")
                if len(still_missing) > 10:
                    msg_lines.append(f"  ... and {len(still_missing)-10} more")

                if action in ("resend_notify", "resend_silent"):
                    # Re-send to Lidarr
                    print(f"[SCHEDULER] Re-sending {len(still_missing)} track(s) to Lidarr")
                    try:
                        _imp.process_missing(still_missing, conf,
                                             lidarr_mode=g.get("lidarr_mode", "album"))
                    except Exception as e:
                        print(f"[SCHEDULER] Follow-up Lidarr error: {e}")

                if action == "notify" or action == "resend_notify":
                    _notify._fire(
                        f"SoundStitch — Discovery Follow-up: Missing Tracks",
                        "\n".join(msg_lines),
                        priority=6,
                    )
                elif action == "resend_silent" and (max_att == 0 or attempt >= max_att):
                    # Only notify on final attempt for silent mode
                    _notify._fire(
                        f"SoundStitch — Discovery Follow-up: Still Missing",
                        "\n".join(msg_lines),
                        priority=7,
                    )

            # Update followup state
            if not still_missing or (max_att != 0 and attempt >= max_att):
                # All found OR out of attempts — clear pending
                fu["pending"]  = []
                fu["check_at"] = None
                if not still_missing:
                    print(f"[SCHEDULER] Follow-up complete — all tracks found")
                else:
                    print(f"[SCHEDULER] Follow-up max attempts reached — giving up")
            else:
                # Schedule next attempt
                next_check = now + timedelta(hours=gap_h)
                fu["pending"]  = still_missing
                fu["check_at"] = _fmt(next_check)
                fu["attempt"]  = attempt + 1
                print(f"[SCHEDULER] Follow-up rescheduled for {fu['check_at']}")

            changed = True

        if changed:
            _cfg.save(conf)

    except Exception as e:
        print(f"[SCHEDULER] Discovery follow-up error: {e}")
    """Auto-create a timestamped backup if the backup schedule is due."""
    try:
        conf = cfg.load()
        bs   = conf.get("backup_schedule", {})
        if not bs.get("enabled"):
            return

        mode = bs.get("mode", "interval")
        now  = datetime.now()
        due  = False

        if mode == "timed":
            # Check if current time matches a scheduled day+time (within the 30s poll window)
            days = bs.get("days", list(range(7)))
            t_str = bs.get("time", "02:00")
            if now.weekday() in days:
                try:
                    h, m = map(int, t_str.split(":"))
                    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
                    # Due if within 90s of the target time and haven't run today
                    if abs((now - target).total_seconds()) <= 90:
                        last_str = bs.get("last_backup")
                        if last_str:
                            last_dt = datetime.strptime(last_str, FMT)
                            # Only fire once per day minimum
                            if (now - last_dt).total_seconds() > 3600:
                                due = True
                        else:
                            due = True
                except ValueError:
                    pass
        else:
            # Interval mode
            interval_secs = int(bs.get("interval_hours", 24)) * 3600
            last_str = bs.get("last_backup")
            if last_str:
                try:
                    last_dt = datetime.strptime(last_str, FMT)
                    if (now - last_dt).total_seconds() >= interval_secs:
                        due = True
                except ValueError:
                    due = True
            else:
                due = True

        if not due:
            return

        print("[SCHEDULER] Backup schedule due — creating backup")
        _create_auto_backup()
        conf = cfg.load()
        conf.setdefault("backup_schedule", {})["last_backup"] = _fmt(now)
        cfg.save(conf)

    except Exception as e:
        print(f"[SCHEDULER] Backup schedule error: {e}")


def _create_auto_backup():
    """Create a timestamped zip backup of /data — mirrors the API backup logic."""
    import zipfile
    from pathlib import Path

    BACKUP_DIR  = "/data/backups"
    MASTER_DIR  = "/data/master"
    PLAYLIST_DIR = "/data/playlists"
    LOG_DIR     = "/data/logs"

    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(BACKUP_DIR) / f"auto_backup_{ts}.zip"

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        secret = Path("/data/.secret")
        if secret.exists():
            zf.write(secret, ".secret")
        config = Path("/data/config.json")
        if config.exists():
            zf.write(config, "config.json")
        for f in Path(MASTER_DIR).rglob("*"):
            if f.is_file():
                zf.write(f, f"master/{f.relative_to(MASTER_DIR)}")
        for f in Path(PLAYLIST_DIR).glob("*.m3u*"):
            zf.write(f, f"playlists/{f.name}")
        log_files = sorted(Path(LOG_DIR).rglob("*.log"), reverse=True)[:20]
        for f in log_files:
            zf.write(f, f"logs/{f.relative_to(LOG_DIR)}")

    print(f"[SCHEDULER] Auto backup created: {out_path.name}")

    # Keep only 10 auto backups
    autos = sorted(Path(BACKUP_DIR).glob("auto_backup_*.zip"), reverse=True)
    for old in autos[10:]:
        old.unlink(missing_ok=True)


def start(log_dir: str):
    global _thread, _stop_event
    _stop_event = threading.Event()
    _thread     = threading.Thread(target=_loop, args=(log_dir,), daemon=True)
    _thread.start()


def stop():
    _stop_event.set()
