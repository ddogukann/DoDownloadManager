import json
import os
import re
import secrets
import threading
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ddm"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "ddm"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
TOKEN_PATH = CONFIG_DIR / "token"
DB_PATH = DATA_DIR / "downloads.db"
BIN_DIR = DATA_DIR / "bin"
TEMP_DIR = DATA_DIR / "tmp"

DEFAULT_USER_AGENT = "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0"

DEFAULT_CAPTURE_EXTS = [
    "zip", "rar", "7z", "tar", "gz", "xz", "bz2", "zst", "iso", "img",
    "deb", "rpm", "appimage", "run", "exe", "msi", "apk", "dmg",
    "mp4", "mkv", "webm", "avi", "mov", "m4v", "flv", "wmv", "ts",
    "mp3", "flac", "wav", "m4a", "ogg", "opus", "aac",
    "pdf", "epub",
]


def _xdg_download_dir():
    user_dirs = CONFIG_DIR.parent / "user-dirs.dirs"
    try:
        text = user_dirs.read_text(encoding="utf-8")
        m = re.search(r'^XDG_DOWNLOAD_DIR="(.+)"', text, re.M)
        if m:
            p = Path(os.path.expandvars(m.group(1).replace("$HOME", str(Path.home()))))
            if p.is_dir():
                return p
    except OSError:
        pass
    for name in ("İndirilenler", "Downloads"):
        p = Path.home() / name
        if p.is_dir():
            return p
    return Path.home() / "Downloads"


DEFAULTS = {
    "language": "auto",
    "download_dir": str(_xdg_download_dir()),
    "category_subfolders": False,
    "max_active": 3,
    "connections": 8,
    "speed_limit_kib": 0,
    "retries": 8,
    "timeout": 30,
    "user_agent": DEFAULT_USER_AGENT,
    "auto_resume": True,
    "run_in_background": True,
    "autostart": False,
    "notifications": True,
    "clipboard_watch": True,
    "confirm_browser": True,
    "capture_exts": DEFAULT_CAPTURE_EXTS,
    "capture_min_kib": 0,
    "video_max_height": 1080,
    "video_container": "mp4",
    "video_own_folder": True,
    "ytdlp_path": "",
    "ytdlp_fragments": 8,
    "detail_on_start": False,
    "detail_auto_close": True,
    "bridge_port": 47821,
    "tray_icon": True,
    "tray_label": True,
    "detail_expanded": False,
    "inhibit_sleep": True,
    "multi_iface": False,
    "multi_ifaces": [],
    "multi_iface_min_mb": 50,
    "rules": [],
    "checksum_auto": True,
    "sched_start": "02:00",
    "sched_end": "07:00",
    "metered_action": "pause",
    "metered_limit_kib": 256,
    "finish_popup": True,
    "update_check": True,
    "update_last": 0,
    "update_skip": "",
    "update_notified": "",
    "vt_api_key": "",
    "vt_enabled": True,
    "vt_url_check": True,
}


class Settings:
    def __init__(self):
        self._lock = threading.RLock()
        self._data = dict(DEFAULTS)
        self._listeners = []
        self.load()

    def load(self):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                for k, v in stored.items():
                    if k in DEFAULTS:
                        self._data[k] = v
        except (OSError, ValueError):
            pass

    def save(self):
        with self._lock:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = SETTINGS_PATH.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, SETTINGS_PATH)

    def get(self, key):
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key))

    def set(self, key, value):
        with self._lock:
            if self._data.get(key) == value:
                return
            self._data[key] = value
        self.save()
        for cb in list(self._listeners):
            try:
                cb(key, value)
            except Exception:
                pass

    def connect(self, cb):
        self._listeners.append(cb)

    def __getitem__(self, key):
        return self.get(key)


def bridge_token():
    try:
        tok = TOKEN_PATH.read_text().strip()
        if tok:
            return tok
    except OSError:
        pass
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(24)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    return tok


def ensure_dirs():
    for d in (CONFIG_DIR, DATA_DIR, BIN_DIR, TEMP_DIR):
        d.mkdir(parents=True, exist_ok=True)
