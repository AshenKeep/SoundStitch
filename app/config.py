"""
Configuration loader/saver with encrypted token storage.

Sensitive fields (access_token, refresh_token, password, api_key, headers_raw)
are encrypted at rest using Fernet symmetric encryption.  The key is derived
from a machine secret stored in /data/.secret (auto-generated on first run)
and is never stored alongside the encrypted data.

Public API is identical to the old config.py — callers always work with
plain-text strings; encryption/decryption is transparent.
"""

import json
import os
import base64
import secrets as _secrets
from pathlib import Path

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/data/config.json"))
SECRET_PATH = Path(os.getenv("SECRET_PATH", "/data/.secret"))

DEFAULTS = {
    "general": {"timezone": ""},
    "auth": {"username": "admin", "password": "changeme"},
    "navidrome": {"url": "", "user": "", "password": ""},
    "lidarr": {"url": "", "api_key": "", "music_path": "/music", "quality_id": 1, "meta_id": 1},
    "musicbrainz": {
        "client_id": "", "client_secret": "", "refresh_token": "",
        "app_name": "SoundStitch", "version": "0.6.14", "contact": ""
    },
    "lastfm": {
        "api_key": "", "api_secret": "", "session_key": "", "username": ""
    },
    "spotify": {
        "enabled": False, "client_id": "", "client_secret": "",
        "redirect_uri": "https://localhost:8443/api/spotify/callback",
        "access_token": "", "refresh_token": "", "token_expiry": 0
    },
    "youtube": {
        "enabled": False, "method": "headers",
        "client_id": "", "client_secret": "",
        "access_token": "", "refresh_token": "", "token_expiry": 0,
        "headers_raw": ""
    },
    "nas": {
        "enabled": False, "device": "", "username": "", "password": "",
        "uid": "1000", "gid": "1000", "vers": "3.0"
    },
    "cooldowns": {
        "mb_cooldown": 1.5, "between_searches": 30,
        "between_artists": 10, "between_albums": 15,
        "interactive_search": 60
    },
    "logs": {
        "max_per_group": 10,
        "verbose": True
    },
    "notifications": {
        "gotify":  {"enabled": False, "url": "", "token": "", "priority": 5},
        "webhook": {"enabled": False, "url": "", "method": "POST",
                    "headers": "", "body_template": ""},
        "on_complete": True,
        "on_error":    True,
        "on_missing":  True,
        "on_stopped":  False,
    },
    "backup_schedule": {
        "enabled":          False,
        "mode":             "interval",   # "interval" or "timed"
        "interval_hours":   24,
        "days":             [0,1,2,3,4,5,6],
        "time":             "02:00",
        "last_backup":      None,
        "next_run":         None,
    },
    "security": {
        "session_timeout_hours": 24,
        "max_login_attempts": 10,
        "lockout_minutes": 60,
    },
    "groups": []
}

# Fields encrypted at rest — (section, field) pairs
_ENCRYPTED_FIELDS: list[tuple] = [
    ("auth",         "password"),
    ("navidrome",    "password"),
    ("lidarr",       "api_key"),
    ("musicbrainz",  "client_secret"),
    ("musicbrainz",  "refresh_token"),
    ("lastfm",       "api_secret"),
    ("lastfm",       "session_key"),
    ("spotify",      "client_secret"),
    ("spotify",      "access_token"),
    ("spotify",      "refresh_token"),
    ("youtube",      "client_secret"),
    ("youtube",      "access_token"),
    ("youtube",      "refresh_token"),
    ("youtube",      "headers_raw"),
]

_PREFIX = "enc:"   # marks encrypted values so plain legacy values pass through

# ── Fernet key ────────────────────────────────────────────────────

_fernet_instance = None

def _get_or_create_secret() -> bytes:
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SECRET_PATH.exists():
        raw = SECRET_PATH.read_bytes().strip()
        try:
            decoded = base64.urlsafe_b64decode(raw)
            if len(decoded) == 32:
                return decoded
        except Exception:
            pass
        try:
            decoded = bytes.fromhex(raw.decode())
            if len(decoded) == 32:
                return decoded
        except Exception:
            pass
    raw = _secrets.token_bytes(32)
    SECRET_PATH.write_bytes(base64.urlsafe_b64encode(raw))
    try:
        SECRET_PATH.chmod(0o600)
    except Exception:
        pass
    return raw


def _fernet():
    global _fernet_instance
    if _fernet_instance is None:
        from cryptography.fernet import Fernet
        raw = _get_or_create_secret()
        key = base64.urlsafe_b64encode(raw)
        _fernet_instance = Fernet(key)
    return _fernet_instance


# ── Encrypt / decrypt ─────────────────────────────────────────────

def _encrypt(plaintext: str) -> str:
    if not plaintext:
        return plaintext
    if plaintext.startswith(_PREFIX):
        return plaintext
    token = _fernet().encrypt(plaintext.encode()).decode()
    return _PREFIX + token


def _decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ciphertext
    if not ciphertext.startswith(_PREFIX):
        return ciphertext   # plain legacy value — return as-is
    try:
        return _fernet().decrypt(ciphertext[len(_PREFIX):].encode()).decode()
    except Exception:
        return ""   # credential lost — caller will trigger re-auth


def _apply(conf: dict, fn) -> dict:
    for section, field in _ENCRYPTED_FIELDS:
        if section in conf and field in conf[section]:
            val = conf[section][field]
            if isinstance(val, str):
                conf[section][field] = fn(val)
    return conf


# ── Public API ────────────────────────────────────────────────────

def load() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                raw = json.load(f)
            merged = deep_merge(DEFAULTS.copy(), raw)
            _apply(merged, _decrypt)
            # Decrypt nested gotify token
            gt = merged.get("notifications", {}).get("gotify", {}).get("token", "")
            if gt:
                merged["notifications"]["gotify"]["token"] = _decrypt(gt)
            return merged
        except Exception as e:
            print(f"[CONFIG] Load error: {e} — using defaults")
    return DEFAULTS.copy()


def save(config: dict):
    import copy
    to_write = copy.deepcopy(config)
    _apply(to_write, _encrypt)
    # Encrypt nested gotify token
    gt = to_write.get("notifications", {}).get("gotify", {}).get("token", "")
    if gt:
        to_write["notifications"]["gotify"]["token"] = _encrypt(gt)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(to_write, f, indent=2)


def deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result
