import os
import threading
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from ..bridge import valid_url
from ..engine import probe
from ..util import category_for, human_size, is_stream_manifest, looks_like_video_page
from ..video import probe_formats, ytdlp_binary

HEIGHTS = [2160, 1440, 1080, 720, 480, 360]


class AddDialog(Adw.Dialog):
    def __init__(self, app, request=None):
        super().__init__()
        self.app = app
        self.settings = app.settings
        self.request = dict(request or {})
        self.custom_dir = None
        self._probe_source = None
        self._probe_token = 0
        self._video_choices = []
        self.set_title("İndirme ekle")
        self.set_content_width(580)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)
        cancel = Gtk.Button(label="Vazgeç")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.start_btn = Gtk.Button(label="İndir")
        self.start_btn.add_css_class("suggested-action")
        self.start_btn.connect("clicked", lambda *_: self._submit(True))
        header.pack_end(self.start_btn)
        later = Gtk.Button(label="Sonra")
        later.set_tooltip_text("Duraklatılmış olarak listeye ekle")
        later.connect("clicked", lambda *_: self._submit(False))
        header.pack_end(later)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        toolbar.set_content(page)
        self.set_child(toolbar)

        main = Adw.PreferencesGroup()
        page.add(main)
        self.url_row = Adw.EntryRow(title="Adres")
        self.url_row.set_text(self.request.get("url") or "")
        self.url_row.connect("changed", self._on_url_changed)
        self.url_row.connect("entry-activated", lambda *_: self._submit(True))
        main.add(self.url_row)

        self.name_row = Adw.EntryRow(title="Dosya adı")
        if self.request.get("filename"):
            self.name_row.set_text(self.request["filename"])
        main.add(self.name_row)

        self.dir_row = Adw.ActionRow(title="Klasör")
        self.dir_row.set_subtitle_selectable(True)
        choose = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
        choose.add_css_class("flat")
        choose.set_tooltip_text("Klasör seç")
        choose.connect("clicked", self._choose_dir)
        self.dir_row.add_suffix(choose)
        main.add(self.dir_row)

        self.info_row = Adw.ActionRow(title="Bilgi", subtitle="—")
        self.spinner = Gtk.Spinner()
        self.info_row.add_suffix(self.spinner)
        main.add(self.info_row)

        self.dup_row = Adw.ActionRow(title="Bu link listede zaten var")
        self.dup_row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
        self.dup_row.set_visible(False)
        main.add(self.dup_row)

        self.resume_row = Adw.ActionRow(title="Yarım kalan indirmeyle eşleşiyor")
        self.resume_row.add_prefix(Gtk.Image(icon_name="emblem-synchronizing-symbolic"))
        resume_btn = Gtk.Button(label="Kaldığı yerden sürdür", valign=Gtk.Align.CENTER)
        resume_btn.add_css_class("suggested-action")
        resume_btn.connect("clicked", self._resume_candidate)
        self.resume_row.add_suffix(resume_btn)
        self.resume_row.set_visible(False)
        self._candidate = None
        main.add(self.resume_row)

        opts = Adw.PreferencesGroup(title="Seçenekler")
        page.add(opts)
        self.video_row = Adw.SwitchRow(title="Video olarak indir",
                                       subtitle="Sayfadaki videoyu yt-dlp ile indirir (YouTube, HLS, DASH…)")
        self.video_row.connect("notify::active", self._on_video_toggled)
        opts.add(self.video_row)

        self.sched_row = Adw.SwitchRow(
            title="Zamanlanmış saatte indir",
            subtitle=f"{self.settings.get('sched_start')}–{self.settings.get('sched_end')} arası · Ayarlar ▸ Güç ve zamanlama")
        opts.add(self.sched_row)

        self.conn_row = Adw.SpinRow.new_with_range(1, 16, 1)
        self.conn_row.set_title("Bağlantı sayısı")
        self.conn_row.set_value(int(self.settings.get("connections") or 8))
        opts.add(self.conn_row)

        self.video_group = Adw.PreferencesGroup(title="Video")
        page.add(self.video_group)
        self.quality_model = Gtk.StringList()
        self.quality_row = Adw.ComboRow(title="Kalite", model=self.quality_model)
        self.video_group.add(self.quality_row)
        self.container_row = Adw.ComboRow(title="Kapsayıcı", model=Gtk.StringList.new(["MP4", "MKV"]))
        self.container_row.set_selected(0 if (self.settings.get("video_container") or "mp4") == "mp4" else 1)
        self.video_group.add(self.container_row)
        self.own_folder_row = Adw.SwitchRow(title="Kendi klasörüne indir", subtitle="Video Adı/Video Adı.mp4")
        self.own_folder_row.set_active(bool(self.settings.get("video_own_folder")))
        self.video_group.add(self.own_folder_row)
        self.quality_row.connect("notify::selected", self._on_quality_changed)
        self._fill_default_qualities()

        self.ytdlp_warn = Adw.ActionRow(title="yt-dlp kurulu değil",
                                        subtitle="Ayarlar ▸ Video bölümünden tek tıkla indirebilirsiniz")
        self.ytdlp_warn.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
        self.video_group.add(self.ytdlp_warn)

        url = self.request.get("url") or ""
        kind = self.request.get("kind")
        auto_video = kind == "video" or (kind is None and (looks_like_video_page(url) or is_stream_manifest(url)))
        self.video_row.set_active(bool(auto_video))
        self._on_video_toggled()
        self._refresh_dir()
        if url:
            self._schedule_probe(0)
        else:
            self._paste_clipboard()

    def _paste_clipboard(self):
        clip = self.app.window.get_clipboard() if self.app.window else None
        if not clip:
            return

        def done(c, res):
            try:
                text = (c.read_text_finish(res) or "").strip()
            except GLib.Error:
                return
            if valid_url(text) and not self.url_row.get_text():
                self.url_row.set_text(text)

        clip.read_text_async(None, done)

    def _fill_default_qualities(self, heights=None, has_audio=True):
        self._video_choices = [{"height": 0, "label": "En iyi"}]
        for h in heights or HEIGHTS:
            self._video_choices.append({"height": h, "label": f"{h}p"})
        if has_audio:
            self._video_choices.append({"audio_only": True, "label": "Yalnız ses (MP3)"})
            self._video_choices.append({"audio_only": True, "audio_format": "m4a", "label": "Yalnız ses (M4A)"})
        self.quality_model.splice(0, self.quality_model.get_n_items(), [c["label"] for c in self._video_choices])
        want = int(self.settings.get("video_max_height") or 1080)
        idx = 0
        for i, c in enumerate(self._video_choices):
            if c.get("height") and c["height"] <= want:
                idx = i
                break
        self.quality_row.set_selected(idx)

    def _on_quality_changed(self, *_):
        choice = self._current_choice()
        self.container_row.set_sensitive(not choice.get("audio_only"))
        if hasattr(self, "own_folder_row"):
            self.own_folder_row.set_sensitive(not choice.get("audio_only"))
        self._refresh_dir()

    def _current_choice(self):
        i = self.quality_row.get_selected()
        if 0 <= i < len(self._video_choices):
            return self._video_choices[i]
        return {"height": 0}

    def _on_video_toggled(self, *_):
        video = self.video_row.get_active()
        self.video_group.set_visible(video)
        self.conn_row.set_visible(not video)
        self.name_row.set_visible(not video)
        self.ytdlp_warn.set_visible(video and not ytdlp_binary(self.settings))
        self._refresh_dir()
        self._schedule_probe(0)

    def _category(self):
        if self.video_row.get_active():
            return "audio" if self._current_choice().get("audio_only") else "video"
        name = self.name_row.get_text().strip() or self.request.get("filename") or self.url_row.get_text().split("?")[0]
        return category_for(name)

    def _refresh_dir(self):
        d = self.custom_dir or str(self.app.manager.default_directory(self._category()))
        self.dir_row.set_subtitle(GLib.markup_escape_text(d))

    def _choose_dir(self, *_):
        dialog = Gtk.FileDialog(title="İndirme klasörü")
        current = self.custom_dir or str(self.app.manager.default_directory(self._category()))
        if os.path.isdir(current):
            dialog.set_initial_folder(Gio.File.new_for_path(current))

        def done(dlg, res):
            try:
                f = dlg.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.custom_dir = f.get_path()
                self._refresh_dir()

        dialog.select_folder(self.get_root(), None, done)

    def _on_url_changed(self, *_):
        url = self.url_row.get_text().strip()
        if self.request.get("kind") is None and url:
            want = looks_like_video_page(url) or is_stream_manifest(url)
            if want != self.video_row.get_active():
                self.video_row.set_active(want)
        self._refresh_dir()
        self._schedule_probe(600)

    def _schedule_probe(self, delay):
        if self._probe_source:
            GLib.source_remove(self._probe_source)
            self._probe_source = None
        self._probe_source = GLib.timeout_add(max(1, delay), self._start_probe)

    def _start_probe(self):
        self._probe_source = None
        url = self.url_row.get_text().strip()
        self._probe_token += 1
        token = self._probe_token
        self.dup_row.set_visible(bool(url and self.app.manager.find_active_by_url(url)))
        if not valid_url(url):
            self.info_row.set_subtitle("Geçerli bir http(s) adresi girin" if url else "—")
            self.start_btn.set_sensitive(False)
            self.spinner.stop()
            return False
        self.start_btn.set_sensitive(True)
        video = self.video_row.get_active()
        if video and not ytdlp_binary(self.settings):
            self.info_row.set_subtitle("yt-dlp gerekli")
            return False
        self.spinner.start()
        self.info_row.set_subtitle("Bilgi alınıyor…")
        headers = dict(self.request.get("headers") or {})
        cookies = self.request.get("cookies")

        def work():
            try:
                if video:
                    result = ("video", probe_formats(url, self.settings, headers, cookies))
                else:
                    result = ("file", probe(url, headers, headers.get("User-Agent") or self.settings.get("user_agent")))
            except Exception as e:
                result = ("error", str(e) or e.__class__.__name__)
            GLib.idle_add(self._probe_done, token, result)

        threading.Thread(target=work, daemon=True).start()
        return False

    def _probe_done(self, token, result):
        if token != self._probe_token:
            return False
        self.spinner.stop()
        kind, data = result
        if kind == "error":
            msg = data
            if "HTTPError" in msg or "Max retries" in msg or "Connection" in msg:
                msg = "Sunucuya ulaşılamadı — yine de deneyebilirsiniz"
            self.info_row.set_subtitle(GLib.markup_escape_text(msg[:300]))
            return False
        if kind == "video":
            bits = [data["title"]]
            if data.get("duration"):
                m, s = divmod(int(data["duration"]), 60)
                h, m = divmod(m, 60)
                bits.append(f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}")
            if data.get("is_live"):
                bits.append("canlı yayın")
            self.info_row.set_subtitle(GLib.markup_escape_text(" · ".join(bits)))
            self.request["title"] = data["title"]
            if data.get("heights"):
                self._fill_default_qualities(data["heights"], data.get("has_audio", True))
            return False
        parts = []
        if data.get("total") is not None:
            parts.append(human_size(data["total"]))
        else:
            parts.append("boyut bilinmiyor")
        parts.append("devam ettirilebilir" if data.get("ranges") else "parçalı indirme desteklenmiyor")
        if data.get("is_html"):
            parts.append("dikkat: link bir web sayfası")
        self.info_row.set_subtitle(GLib.markup_escape_text(" · ".join(parts)))
        if not self.name_row.get_text().strip() and data.get("filename"):
            self.name_row.set_text(data["filename"])
        if not data.get("ranges"):
            self.conn_row.set_value(1)
        cand = self.app.manager.find_resume_candidate(
            self.name_row.get_text().strip() or data.get("filename"), data.get("total"))
        if cand is None and data.get("filename"):
            cand = self.app.manager.find_resume_candidate(data["filename"], data.get("total"))
        self._candidate = cand
        if cand:
            pct = int((cand.get("downloaded") or 0) * 100 / cand["total"]) if cand.get("total") else 0
            self.resume_row.set_subtitle(GLib.markup_escape_text(
                f"{cand.get('filename')} · %{pct} indirilmişti ({human_size(cand.get('downloaded'))}). "
                "Bu linkle yeniden indirmek yerine kaldığı yerden devam edebilirsin."))
        self.resume_row.set_visible(bool(cand))
        self._refresh_dir()
        return False

    def _resume_candidate(self, *_):
        cand = self._candidate
        if not cand:
            return
        url = self.url_row.get_text().strip()
        self.app.manager.refresh_url(cand["id"], url, dict(self.request.get("headers") or {}),
                                     self.request.get("page_url"))
        self.close()
        if self.app.window:
            self.app.window.toast(f"{cand.get('filename')} yeni linkle kaldığı yerden sürüyor")

    def _submit(self, start):
        url = self.url_row.get_text().strip()
        if not valid_url(url):
            self.url_row.add_css_class("error")
            return
        headers = dict(self.request.get("headers") or {})
        if self.video_row.get_active():
            choice = self._current_choice()
            options = {
                "height": choice.get("height", 0),
                "audio_only": bool(choice.get("audio_only")),
                "audio_format": choice.get("audio_format") or "mp3",
                "container": "mp4" if self.container_row.get_selected() == 0 else "mkv",
                "title": self.request.get("title"),
                "own_folder": self.own_folder_row.get_active() and not choice.get("audio_only"),
            }
            if self.request.get("cookies"):
                options["cookies"] = self.request["cookies"]
            if self.sched_row.get_active():
                options["scheduled"] = True
            headers.pop("Cookie", None)
            rec = self.app.manager.add(url, kind="video", directory=self.custom_dir, headers=headers,
                                       options=options, start=start, page_url=self.request.get("page_url"))
        else:
            name = self.name_row.get_text().strip() or None
            if self.custom_dir:
                Path(self.custom_dir).mkdir(parents=True, exist_ok=True)
            rec = self.app.manager.add(url, kind="file", filename=name, directory=self.custom_dir,
                                       headers=headers, start=start, page_url=self.request.get("page_url"),
                                       connections=int(self.conn_row.get_value()),
                                       options={"scheduled": True} if self.sched_row.get_active() else None)
        self.close()
        if start and self.settings.get("detail_on_start"):
            self.app.show_details(rec["id"])
