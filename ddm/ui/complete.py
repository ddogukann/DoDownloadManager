import os

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..util import human_eta, human_size, human_speed
from .items import vt_display
from .widgets import SpeedGraph, chip, set_chip, stat_tile

VT_STYLE = {"ok": "success", "warn": "warning", "bad": "error", "busy": "accent", "unknown": "dim"}


class CompleteWindow(Adw.Window):
    def __init__(self, app, did, stats):
        super().__init__(application=app)
        self.app = app
        self.did = did
        self.stats = stats or {}
        self.set_default_size(440, -1)
        self.set_resizable(False)
        self.set_title("İndirme tamamlandı")

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar(show_title=False)
        tv.add_top_bar(header)
        self.set_content(tv)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_start=24, margin_end=24,
                      margin_bottom=22)
        tv.set_content(box)

        badge = Gtk.Image(icon_name="object-select-symbolic", pixel_size=30, halign=Gtk.Align.CENTER)
        badge.add_css_class("ddm-done-badge")
        box.append(badge)
        title = Gtk.Label(label="İndirme tamamlandı")
        title.add_css_class("title-2")
        box.append(title)
        self.name = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, justify=Gtk.Justification.CENTER,
                              max_width_chars=40, lines=3, ellipsize=Pango.EllipsizeMode.END)
        self.name.add_css_class("heading")
        box.append(self.name)
        self.folder = Gtk.Label(ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=48)
        self.folder.add_css_class("caption")
        self.folder.add_css_class("dim-label")
        box.append(self.folder)

        grid = Gtk.Grid(column_spacing=8, row_spacing=8, column_homogeneous=True, margin_top=4)
        t1, self.v_size = stat_tile("Boyut")
        t2, self.v_time = stat_tile("Süre")
        self.cap_time = t2.get_last_child()
        t3, self.v_avg = stat_tile("Ortalama hız")
        t4, self.v_peak = stat_tile("En yüksek hız")
        grid.attach(t1, 0, 0, 1, 1)
        grid.attach(t2, 1, 0, 1, 1)
        grid.attach(t3, 0, 1, 1, 1)
        grid.attach(t4, 1, 1, 1, 1)
        box.append(grid)

        self.graph = SpeedGraph(64)
        self.graph.add_css_class("ddm-graph-frame")
        box.append(self.graph)

        chips = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        self.chip_vt = chip()
        self.chip_check = chip()
        self.chip_post = chip()
        for c in (self.chip_vt, self.chip_check, self.chip_post):
            chips.append(c)
        chips.set_homogeneous(False)
        box.append(chips)

        buttons = Gtk.Box(spacing=8, homogeneous=True, margin_top=4)
        folder_btn = Gtk.Button(label="Klasörü aç")
        folder_btn.add_css_class("pill")
        folder_btn.connect("clicked", self._open_folder)
        open_btn = Gtk.Button(label="Dosyayı aç")
        self.open_btn = open_btn
        self.folder_btn = folder_btn
        open_btn.add_css_class("pill")
        open_btn.add_css_class("suggested-action")
        open_btn.connect("clicked", self._open_file)
        buttons.append(folder_btn)
        buttons.append(open_btn)
        box.append(buttons)

        foot = Gtk.Box(spacing=6)
        self.never = Gtk.CheckButton(label="Bir daha gösterme", hexpand=True)
        self.never.add_css_class("caption")
        self.never.connect("toggled", lambda b: app.settings.set("finish_popup", not b.get_active()))
        foot.append(self.never)
        details = Gtk.Button(label="Ayrıntılar")
        details.add_css_class("flat")
        details.add_css_class("caption")
        details.connect("clicked", self._details)
        foot.append(details)
        box.append(foot)

        self.set_focus(open_btn)
        self._fill()
        self._timer = GLib.timeout_add(1000, self._refresh)
        self.connect("close-request", self._on_close)

    def _on_close(self, *_):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None
        self.app.complete_windows.pop(self.did, None)
        return False

    def _open_folder(self, *_):
        rec = self.app.manager.get(self.did) or {}
        dest = (rec.get("post") or {}).get("dest")
        if dest and os.path.isdir(dest):
            self.app._launch(dest)
        else:
            self.app.open_folder(self.did)
        self.close()

    def _open_file(self, *_):
        rec = self.app.manager.get(self.did) or {}
        post = rec.get("post") or {}
        if post.get("action") == "extracted" and post.get("dest") and os.path.isdir(post["dest"]):
            self.app._launch(post["dest"])
        else:
            self.app.open_file(self.did)
        self.close()

    def _details(self, *_):
        self.app.show_details(self.did)
        self.close()

    def _fill(self):
        rec = self.app.manager.get(self.did)
        if not rec:
            return
        path = rec.get("path") or ""
        self.name.set_label(rec.get("filename") or os.path.basename(path) or rec["url"])
        self.folder.set_label(os.path.dirname(path) or rec.get("directory") or "")
        self.folder.set_tooltip_text(path or None)
        total = rec.get("total") or 0
        elapsed = rec.get("elapsed") or 0
        self.v_size.set_label(human_size(total) if total else "—")
        self.v_time.set_label(human_eta(elapsed) if elapsed else "—")
        self.v_avg.set_label(human_speed(total / elapsed) if total and elapsed else "—")
        peak = self.stats.get("peak") or 0
        self.v_peak.set_label(human_speed(peak) if peak else "—")
        hist = self.app.speed_history(self.did)
        self.graph.set_samples(hist)
        self.graph.set_visible(len(hist) >= 4)
        conns = self.stats.get("max_conns") or 0
        if conns > 1 and rec["kind"] != "video":
            self.cap_time.set_label(f"Süre · {conns} bağlantı")
        elif rec["kind"] == "video":
            self.cap_time.set_label("Süre · yt-dlp")
        self._fill_vt(rec)

    def _fill_vt(self, rec):
        vt = rec.get("vt") or {}
        text, level, tip = vt_display(vt, rec["status"])
        if vt.get("mismatch"):
            set_chip(self.chip_vt, "", None)
        else:
            set_chip(self.chip_vt, f"VirusTotal {text}" if text else "", VT_STYLE.get(level))
            self.chip_vt.set_tooltip_text(tip or None)
        post = rec.get("post") or {}
        ck = post.get("checksum")
        name = vt.get("checksum_file") or "checksum"
        if ck == "ok":
            set_chip(self.chip_check, f"{name} ✓", "success")
        elif ck == "bad":
            set_chip(self.chip_check, f"{name} ✕ uyuşmuyor", "error")
        elif ck == "searching":
            set_chip(self.chip_check, "checksum aranıyor…", "dim")
        else:
            set_chip(self.chip_check, "", None)
        self.chip_check.set_tooltip_text(vt.get("checksum_src") or None)
        act = post.get("action")
        if act == "extracted":
            self.open_btn.set_label("Açılan klasörü aç")
            self.folder_btn.set_label("Konumu göster")
            set_chip(self.chip_post, "arşiv açıldı" + (" · arşiv silindi" if post.get("archive_deleted") else ""), "accent")
            self.chip_post.set_tooltip_text(post.get("dest"))
        elif act == "extracting":
            set_chip(self.chip_post, "arşiv açılıyor…", "dim")
        elif act in ("skipped", "error"):
            set_chip(self.chip_post, "otomatik işlem yapılmadı", "warning")
            self.chip_post.set_tooltip_text(post.get("reason"))
        else:
            set_chip(self.chip_post, "", None)
        if ck == "searching" or act == "extracting":
            return "busy"
        return level

    def _refresh(self):
        rec = self.app.manager.get(self.did)
        if not rec:
            self.close()
            return False
        level = self._fill_vt(rec)
        if level in ("ok", "bad", "warn", "unknown", "") and level != "busy":
            vt = rec.get("vt") or {}
            if vt.get("state") not in ("hashing", "queued", "checking"):
                self._timer = None
                return False
        return True
