import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .i18n import _, decimal

CATEGORIES = [
    ("all", _("Tümü"), "folder-download-symbolic"),
    ("active", _("Devam eden"), "media-playback-start-symbolic"),
    ("done", _("Tamamlanan"), "object-select-symbolic"),
    ("video", _("Video"), "video-x-generic-symbolic"),
    ("audio", _("Müzik"), "audio-x-generic-symbolic"),
    ("archive", _("Arşiv"), "package-x-generic-symbolic"),
    ("document", _("Belge"), "x-office-document-symbolic"),
    ("program", _("Program"), "application-x-executable-symbolic"),
    ("image", _("Resim"), "image-x-generic-symbolic"),
    ("other", _("Diğer"), "text-x-generic-symbolic"),
]

CATEGORY_FOLDERS = {
    "video": _("Video"),
    "audio": _("Müzik"),
    "archive": _("Arşiv"),
    "document": _("Belge"),
    "program": _("Program"),
    "image": _("Resim"),
    "other": _("Diğer"),
}

_EXT_MAP = {
    "video": {"mp4", "mkv", "webm", "avi", "mov", "m4v", "flv", "wmv", "ts", "mpg", "mpeg", "3gp", "m2ts"},
    "audio": {"mp3", "m4a", "flac", "wav", "ogg", "opus", "aac", "wma", "alac", "aiff"},
    "archive": {"zip", "rar", "7z", "tar", "gz", "tgz", "xz", "bz2", "zst", "iso", "img", "lz", "lzma", "cab"},
    "document": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "txt", "rtf", "epub", "csv", "md"},
    "program": {"deb", "rpm", "appimage", "run", "sh", "exe", "msi", "apk", "dmg", "flatpak", "flatpakref", "snap", "jar"},
    "image": {"jpg", "jpeg", "png", "gif", "webp", "svg", "heic", "bmp", "tif", "tiff", "avif", "raw"},
}

_INVALID = re.compile(r'[\x00-\x1f/\\]')


def category_for(name):
    ext = Path(name or "").suffix.lower().lstrip(".")
    for cat, exts in _EXT_MAP.items():
        if ext in exts:
            return cat
    return "other"


def sanitize_filename(name, fallback=None):
    fallback = fallback or _("indirme")
    name = _INVALID.sub("_", (name or "").strip()).strip(". ")
    if not name:
        name = fallback
    if len(name.encode("utf-8")) > 240:
        stem, ext = os.path.splitext(name)
        stem = stem.encode("utf-8")[: 240 - len(ext.encode("utf-8"))].decode("utf-8", "ignore")
        name = stem + ext
    return name


def filename_from_url(url):
    path = urlsplit(url).path
    base = unquote(path.rsplit("/", 1)[-1]) if path else ""
    return sanitize_filename(base) if base else ""


def filename_from_disposition(value):
    if not value:
        return ""
    m = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", value, re.I)
    if m:
        enc = m.group(1) or "utf-8"
        try:
            return sanitize_filename(unquote(m.group(2).strip().strip('"'), encoding=enc))
        except LookupError:
            return sanitize_filename(unquote(m.group(2).strip().strip('"')))
    m = re.search(r'filename\s*=\s*"((?:[^"\\]|\\.)*)"', value, re.I)
    if m:
        raw = m.group(1).replace('\\"', '"')
    else:
        m = re.search(r"filename\s*=\s*([^;]+)", value, re.I)
        if not m:
            return ""
        raw = m.group(1).strip()
    try:
        raw = raw.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return sanitize_filename(unquote(raw))


def unique_path(directory, name):
    directory = Path(directory)
    candidate = directory / name
    if not candidate.exists() and not Path(str(candidate) + ".part").exists():
        return candidate
    stem, ext = os.path.splitext(name)
    if stem.endswith(".tar"):
        stem, ext = stem[:-4], ".tar" + ext
    i = 1
    while True:
        candidate = directory / f"{stem} ({i}){ext}"
        if not candidate.exists() and not Path(str(candidate) + ".part").exists():
            return candidate
        i += 1


def human_size(n):
    if n is None or n < 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f} {u}" if u == "B" else decimal(f"{f:.1f} {u}")
        f /= 1024
    return str(n)


def human_speed(bps):
    if not bps:
        return "—"
    return human_size(bps) + "/s"


def human_eta(seconds):
    if seconds is None or seconds < 0 or seconds == float("inf"):
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return _("{h} sa {m:02d} dk").format(h=h, m=m)
    if m:
        return _("{m} dk {s:02d} sn").format(m=m, s=s)
    return _("{s} sn").format(s=s)


VIDEO_SITE_HINTS = (
    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "twitch.tv", "x.com", "twitter.com",
    "instagram.com", "tiktok.com", "facebook.com", "reddit.com", "soundcloud.com", "bilibili.com",
    "streamable.com", "kick.com", "rumble.com", "bandcamp.com",
)


def looks_like_video_page(url):
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in VIDEO_SITE_HINTS)


def is_stream_manifest(url):
    path = urlsplit(url).path.lower()
    return path.endswith(".m3u8") or path.endswith(".mpd")
