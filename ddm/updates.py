import os
import re
from pathlib import Path

import requests

from . import REPO, VERSION

API = os.environ.get("DDM_UPDATE_API", f"https://api.github.com/repos/{REPO}/releases/latest")
CHECK_INTERVAL = 24 * 3600


def parse_version(text):
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


def is_newer(tag, current=VERSION):
    latest, mine = parse_version(tag), parse_version(current)
    return bool(latest and mine and latest > mine)


def installed_from_deb():
    return str(Path(__file__).resolve()).startswith("/usr/lib/")


def fetch_latest(timeout=10):
    r = requests.get(API, timeout=timeout, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"DDM/{VERSION}"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name") or ""
    version = parse_version(tag)
    assets = [{"name": a.get("name") or "", "url": a.get("browser_download_url") or "", "size": a.get("size") or 0}
              for a in data.get("assets") or []]
    return {
        "tag": tag,
        "version": ".".join(map(str, version)) if version else tag,
        "url": data.get("html_url") or f"https://github.com/{REPO}/releases/latest",
        "notes": data.get("body") or "",
        "assets": assets,
        "deb": next((a for a in assets if a["name"].endswith(".deb")), None),
    }


def plain_notes(text, limit=1500):
    lines = []
    for line in (text or "").replace("\r", "").splitlines():
        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        line = re.sub(r"`([^`]+)`", r"\1", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        line = re.sub(r"^\s*[-*]\s+", "• ", line)
        lines.append(line.rstrip())
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out if len(out) <= limit else out[:limit].rsplit("\n", 1)[0] + "\n…"
