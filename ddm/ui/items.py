import time

from gi.repository import GObject

from ..i18n import _, pct
from ..util import CATEGORIES, human_eta, human_size, human_speed

STATUS_TEXT = {
    "queued": _("Sırada"),
    "running": _("İndiriliyor"),
    "paused": _("Duraklatıldı"),
    "done": _("Tamamlandı"),
    "error": _("Hata"),
}

CATEGORY_ICONS = {key: icon for key, _label, icon in CATEGORIES}


def vt_display(vt, status):
    vt = vt or {}
    state = vt.get("state")
    stats = vt.get("stats") or {}
    verdict = vt.get("verdict")
    if vt.get("mismatch"):
        src = vt.get("checksum_file") or _("sunucunun bildirdiği değer")
        return "≠ hash", "bad", _("Dosyanın SHA-256 değeri {src} ile uyuşmuyor; dosya bozuk ya da değiştirilmiş olabilir").format(src=src)
    if verdict and state not in ("hashing",):
        n, total = stats.get("malicious", 0), stats.get("total", 0)
        if verdict == "clean":
            return f"✓ 0/{total}", "ok", _("VirusTotal: {total} motorun hiçbiri zararlı bulmadı").format(total=total)
        if verdict == "suspicious":
            return f"⚠ {n + stats.get('suspicious', 0)}/{total}", "warn", \
                _("VirusTotal: {n} motor zararlı, {s} motor şüpheli buldu").format(n=n, s=stats.get("suspicious", 0))
        if verdict == "malicious":
            return f"✕ {n}/{total}", "bad", _("VirusTotal: {n}/{total} motor zararlı buldu").format(n=n, total=total)
    busy = {"hashing": _("Hash…"), "queued": _("Sırada…"), "checking": _("Sorgu…"), "uploading": _("Yükleniyor…"),
            "analyzing": _("Taranıyor…")}
    if state in busy:
        return busy[state], "busy", _("VirusTotal kontrolü sürüyor")
    if state == "unknown":
        return _("? kayıtsız"), "unknown", _("Bu dosya VirusTotal'da kayıtlı değil. Sağ tık ▸ VirusTotal'a yükle ve tara")
    if state == "error":
        return _("! hata"), "warn", vt.get("error") or _("VirusTotal kontrolü başarısız")
    uv = vt.get("url_verdict")
    if uv == "malicious":
        return "✕ link", "bad", _("Link/alan adı VirusTotal'da zararlı olarak işaretli ({n} motor)").format(
            n=(vt.get("url_stats") or {}).get("malicious", 0))
    if uv == "suspicious":
        return "⚠ link", "warn", _("Link/alan adı VirusTotal'da şüpheli olarak işaretli")
    if vt.get("url_state") == "checking":
        return _("Link…"), "busy", _("Link VirusTotal'da kontrol ediliyor")
    if uv == "clean":
        return "✓ link", "ok", _("Link/alan adı temiz; dosya inince kontrol edilecek")
    return "", "", ""


def format_created(ts):
    if not ts:
        return ""
    lt = time.localtime(ts)
    now = time.localtime()
    if lt.tm_year == now.tm_year and lt.tm_yday == now.tm_yday:
        return time.strftime("%H:%M", lt)
    return time.strftime(_("%d.%m.%Y %H:%M"), lt)


class DownloadItem(GObject.Object):
    __gtype_name__ = "DDMDownloadItem"

    did = GObject.Property(type=str, default="")
    name = GObject.Property(type=str, default="")
    url = GObject.Property(type=str, default="")
    kind = GObject.Property(type=str, default="file")
    status = GObject.Property(type=str, default="")
    status_text = GObject.Property(type=str, default="")
    category = GObject.Property(type=str, default="other")
    icon_name = GObject.Property(type=str, default="text-x-generic-symbolic")
    total = GObject.Property(type=GObject.TYPE_INT64, default=0)
    size_text = GObject.Property(type=str, default="—")
    progress = GObject.Property(type=float, default=0.0)
    progress_text = GObject.Property(type=str, default="")
    speed = GObject.Property(type=float, default=0.0)
    speed_text = GObject.Property(type=str, default="")
    eta_text = GObject.Property(type=str, default="")
    created = GObject.Property(type=float, default=0.0)
    created_text = GObject.Property(type=str, default="")
    path = GObject.Property(type=str, default="")
    error = GObject.Property(type=str, default="")
    vt_text = GObject.Property(type=str, default="")
    vt_level = GObject.Property(type=str, default="")
    vt_tip = GObject.Property(type=str, default="")
    sha256 = GObject.Property(type=str, default="")

    def __init__(self, rec):
        super().__init__()
        self.did = rec["id"]
        self.update(rec, None)

    def _set(self, prop, value):
        if self.get_property(prop) != value:
            self.set_property(prop, value)

    def update(self, rec, snap):
        if rec is not None:
            self.record = rec
        rec = self.record
        status = rec["status"]
        name = rec.get("filename") or rec["url"].rsplit("/", 1)[-1] or rec["url"]
        self._set("name", name)
        self._set("url", rec["url"])
        self._set("kind", rec.get("kind") or "file")
        self._set("status", status)
        self._set("category", rec.get("category") or "other")
        icon = "video-x-generic-symbolic" if rec.get("kind") == "video" and rec.get("category") != "audio" \
            else CATEGORY_ICONS.get(rec.get("category") or "other", "text-x-generic-symbolic")
        self._set("icon_name", icon)
        self._set("path", rec.get("path") or "")
        self._set("error", rec.get("error") or "")
        vt_text, vt_level, vt_tip = vt_display(rec.get("vt"), status)
        self._set("vt_text", vt_text)
        self._set("vt_level", vt_level)
        self._set("vt_tip", vt_tip)
        self._set("sha256", rec.get("sha256") or "")
        self._set("created", float(rec.get("created") or 0))
        self._set("created_text", format_created(rec.get("created")))
        total = (snap or {}).get("total") if snap and snap.get("total") else rec.get("total")
        done = (snap or {}).get("downloaded", rec.get("downloaded") or 0) if snap else (rec.get("downloaded") or 0)
        if status == "done" and total:
            done = total
        total = int(total or 0)
        self._set("total", total)
        if total:
            frac = max(0.0, min(1.0, done / total))
            self._set("size_text", human_size(total) if status == "done" else f"{human_size(done)} / {human_size(total)}")
        else:
            frac = 1.0 if status == "done" else 0.0
            self._set("size_text", human_size(done) if done else "—")
        self._set("progress", frac)
        self._set("progress_text", pct(f"{frac * 100:.0f}") if total or status == "done" else "")
        speed = float((snap or {}).get("speed") or 0.0) if status == "running" else 0.0
        self._set("speed", speed)
        self._set("speed_text", human_speed(speed) if status == "running" and speed else "")
        eta = None
        if status == "running":
            if snap and snap.get("eta"):
                eta = snap["eta"]
            elif total and speed > 0:
                eta = (total - done) / speed
        self._set("eta_text", human_eta(eta) if eta is not None else "")
        if status == "running" and snap and snap.get("phase") == "birleştiriliyor":
            st = _("Birleştiriliyor")
        elif status == "running" and snap and not snap.get("downloaded") and not speed:
            st = _("Bağlanıyor")
        elif status == "queued" and (rec.get("options") or {}).get("scheduled"):
            st = _("Zamanlandı")
        elif status == "error" and (rec.get("options") or {}).get("expired"):
            st = _("Link süresi doldu")
        elif status == "error":
            st = _("Hata: ") + (rec.get("error") or "")
        else:
            st = STATUS_TEXT.get(status, status)
        self._set("status_text", st)
