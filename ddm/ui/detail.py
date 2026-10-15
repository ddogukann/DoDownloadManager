from urllib.parse import urlsplit

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..util import human_eta, human_size, human_speed
from ..virustotal import file_link
from .items import STATUS_TEXT, vt_display
from .widgets import SegmentBar, SpeedGraph, chip, set_chip

SUMMARY_WIDTH = 560
DETAIL_HEIGHT = 720

VT_STYLE = {"ok": "success", "warn": "warning", "bad": "error", "busy": "accent", "unknown": "dim"}
STATUS_STYLE = {"running": "accent", "queued": "dim", "paused": "warning", "done": "success", "error": "error"}


class ConnRow(Gtk.Box):
    def __init__(self):
        super().__init__(spacing=12, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        self.num = Gtk.Label(width_chars=2, xalign=1)
        self.num.add_css_class("numeric")
        self.num.add_css_class("dim-label")
        self.bar = Gtk.ProgressBar(hexpand=True, valign=Gtk.Align.CENTER)
        self.bar.add_css_class("ddm-thin")
        self.amount = Gtk.Label(xalign=1, width_chars=17)
        self.amount.add_css_class("numeric")
        self.amount.add_css_class("caption")
        self.state = Gtk.Label(xalign=0, width_chars=18, ellipsize=Pango.EllipsizeMode.END)
        self.state.add_css_class("caption")
        self.state.add_css_class("dim-label")
        for w in (self.num, self.bar, self.amount, self.state):
            self.append(w)

    def set(self, num, frac, amount, state):
        self.num.set_label(num)
        self.bar.set_fraction(max(0.0, min(1.0, frac)))
        self.amount.set_label(amount)
        self.state.set_label(state)


class DetailWindow(Adw.Window):
    def __init__(self, app, did):
        super().__init__(application=app)
        self.app = app
        self.did = did
        self._last_status = None
        self._conn_rows = []
        self.expanded = bool(app.settings.get("detail_expanded"))
        self.set_default_size(SUMMARY_WIDTH, DETAIL_HEIGHT if self.expanded else -1)
        self.set_title("İndirme")

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        switch = Gtk.Box(css_classes=["linked"])
        self.btn_summary = Gtk.ToggleButton(label="Özet")
        self.btn_detail = Gtk.ToggleButton(label="Ayrıntı", group=self.btn_summary)
        switch.append(self.btn_summary)
        switch.append(self.btn_detail)
        header.set_title_widget(switch)
        menu = Gio.Menu()
        menu.append("Linki kopyala", "dw.copy-url")
        menu.append("SHA-256 kopyala", "dw.copy-hash")
        menu.append("VirusTotal'da aç", "dw.vt-open")
        header.pack_end(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, tooltip_text="Diğer"))
        tv.add_top_bar(header)
        self._install_actions()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tv.set_content(root)
        self.set_content(tv)

        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=22, margin_end=22,
                       margin_top=6, margin_bottom=14)
        root.append(hero)
        top = Gtk.Box(spacing=12)
        self.icon = Gtk.Image(pixel_size=40, valign=Gtk.Align.CENTER)
        self.icon.add_css_class("ddm-file-icon")
        top.append(self.icon)
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self.name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.name.add_css_class("title-4")
        self.host = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.host.add_css_class("caption")
        self.host.add_css_class("dim-label")
        names.append(self.name)
        names.append(self.host)
        top.append(names)
        hero.append(top)

        nums = Gtk.Box(spacing=12, margin_top=6)
        self.pct = Gtk.Label(xalign=0, valign=Gtk.Align.END)
        self.pct.add_css_class("ddm-hero")
        nums.append(self.pct)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, hexpand=True, halign=Gtk.Align.END,
                        valign=Gtk.Align.END)
        self.speed = Gtk.Label(xalign=1)
        self.speed.add_css_class("ddm-speed")
        self.eta = Gtk.Label(xalign=1)
        self.eta.add_css_class("dim-label")
        self.eta.add_css_class("numeric")
        right.append(self.speed)
        right.append(self.eta)
        nums.append(right)
        hero.append(nums)

        self.bar = SegmentBar(12)
        hero.append(self.bar)

        meta = Gtk.Box(spacing=6)
        self.amount = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.amount.add_css_class("numeric")
        self.amount.add_css_class("caption")
        self.amount.add_css_class("dim-label")
        meta.append(self.amount)
        self.chip_vt = chip()
        self.chip_status = chip()
        meta.append(self.chip_vt)
        meta.append(self.chip_status)
        hero.append(meta)

        self.revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN, vexpand=True,
                                     transition_duration=180)
        root.append(self.revealer)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.revealer.set_child(scroller)
        detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_start=18, margin_end=18,
                         margin_top=4, margin_bottom=18)
        scroller.set_child(detail)

        graph_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        graph_card.add_css_class("card")
        graph_card.add_css_class("ddm-card-pad")
        gtitle = Gtk.Box()
        gl = Gtk.Label(label="Hız", xalign=0, hexpand=True)
        gl.add_css_class("heading")
        self.graph_note = Gtk.Label(xalign=1)
        self.graph_note.add_css_class("caption")
        self.graph_note.add_css_class("dim-label")
        gtitle.append(gl)
        gtitle.append(self.graph_note)
        graph_card.append(gtitle)
        self.graph = SpeedGraph(80)
        graph_card.append(self.graph)
        detail.append(graph_card)

        info = Adw.PreferencesGroup(title="Dosya")
        detail.append(info)
        self.row_path = Adw.ActionRow(title="Konum", subtitle_selectable=True)
        b = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Klasörü aç")
        b.add_css_class("flat")
        b.connect("clicked", lambda *_: app.open_folder(self.did))
        self.row_path.add_suffix(b)
        self.row_url = Adw.ActionRow(title="Adres", subtitle_selectable=True)
        b = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Kopyala")
        b.add_css_class("flat")
        b.connect("clicked", lambda *_: self._copy("url"))
        self.row_url.add_suffix(b)
        self.row_resume = Adw.ActionRow(title="Bağlantı")
        self.row_sha = Adw.ActionRow(title="SHA-256", subtitle_selectable=True)
        b = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Kopyala")
        b.add_css_class("flat")
        b.connect("clicked", lambda *_: self._copy("sha"))
        self.row_sha.add_suffix(b)
        self.row_check = Adw.ActionRow(title="Doğrulama")
        self.row_post = Adw.ActionRow(title="Otomatik işlem")
        b = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Açılan klasörü göster")
        b.add_css_class("flat")
        b.connect("clicked", self._open_post_dest)
        self.post_btn = b
        self.row_post.add_suffix(b)
        for r in (self.row_path, self.row_url, self.row_resume, self.row_sha, self.row_check, self.row_post):
            r.add_css_class("property")
            info.add(r)

        vt = Adw.PreferencesGroup(title="VirusTotal")
        detail.append(vt)
        self.row_vt = Adw.ActionRow(title="Sonuç")
        self.row_vt.add_css_class("property")
        vt_btns = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.vt_open = Gtk.Button(label="Aç")
        self.vt_open.connect("clicked", lambda *_: app.vt_open(self.did))
        self.vt_upload = Gtk.Button(label="Yükle ve tara")
        self.vt_upload.connect("clicked", lambda *_: app.vt_upload(self.did))
        self.vt_recheck = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Yeniden kontrol et")
        self.vt_recheck.connect("clicked", lambda *_: app.manager.vt_recheck(self.did))
        for w in (self.vt_open, self.vt_upload, self.vt_recheck):
            w.add_css_class("flat")
            vt_btns.append(w)
        self.row_vt.add_suffix(vt_btns)
        self.row_url_vt = Adw.ActionRow(title="Link")
        self.row_url_vt.add_css_class("property")
        vt.add(self.row_vt)
        vt.add(self.row_url_vt)

        self.conn_group = Adw.PreferencesGroup(title="Bağlantılar")
        detail.append(self.conn_group)
        self.conn_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.conn_list.add_css_class("boxed-list")
        self.conn_group.add(self.conn_list)

        actions = Gtk.Box(spacing=8, margin_start=18, margin_end=18, margin_top=6, margin_bottom=18)
        root.append(actions)
        self.folder_btn = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Klasörü aç")
        self.folder_btn.add_css_class("circular")
        self.folder_btn.connect("clicked", lambda *_: app.open_folder(self.did))
        actions.append(self.folder_btn)
        actions.append(Gtk.Box(hexpand=True))
        self.cancel_btn = Gtk.Button()
        self.cancel_btn.add_css_class("pill")
        self.cancel_btn.connect("clicked", self._cancel)
        actions.append(self.cancel_btn)
        self.toggle_btn = Gtk.Button()
        self.toggle_btn.add_css_class("pill")
        self.toggle_btn.add_css_class("suggested-action")
        self.toggle_btn.connect("clicked", self._toggle)
        actions.append(self.toggle_btn)

        (self.btn_detail if self.expanded else self.btn_summary).set_active(True)
        self.revealer.set_reveal_child(self.expanded)
        self.revealer.set_visible(self.expanded)
        self.btn_detail.connect("toggled", self._on_mode)
        self.revealer.connect("notify::child-revealed", self._on_revealed)

        self.set_focus(self.toggle_btn)
        self._timer = GLib.timeout_add(500, self._tick)
        self.connect("close-request", self._on_close)
        self._tick()

    def _install_actions(self):
        group = Gio.SimpleActionGroup()
        for name, cb in (("copy-url", lambda *_: self._copy("url")), ("copy-hash", lambda *_: self._copy("sha")),
                         ("vt-open", lambda *_: self.app.vt_open(self.did))):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            group.add_action(a)
        self.insert_action_group("dw", group)
        self._actions = group

    def _on_mode(self, btn):
        self.expanded = btn.get_active()
        self.app.settings.set("detail_expanded", self.expanded)
        if self.expanded:
            self.revealer.set_visible(True)
            self.set_default_size(max(self.get_width(), SUMMARY_WIDTH), DETAIL_HEIGHT)
            self.revealer.set_reveal_child(True)
        else:
            self.revealer.set_reveal_child(False)

    def _on_revealed(self, rev, _p):
        if not rev.get_child_revealed() and not self.expanded:
            rev.set_visible(False)
            self.set_default_size(max(self.get_width(), SUMMARY_WIDTH), -1)
            GLib.idle_add(self._shrink)

    def _shrink(self):
        self.set_default_size(self.get_width(), 1)
        return False

    def _open_post_dest(self, *_):
        rec = self.app.manager.get(self.did)
        dest = ((rec or {}).get("post") or {}).get("dest")
        if dest:
            self.app._launch(dest)

    def _copy(self, what):
        rec = self.app.manager.get(self.did)
        if not rec:
            return
        text = rec["url"] if what == "url" else rec.get("sha256")
        if text:
            self.get_clipboard().set(text)

    def _on_close(self, *_):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None
        self.app.detail_windows.pop(self.did, None)
        return False

    def _cancel(self, *_):
        rec = self.app.manager.get(self.did)
        if rec and rec["status"] != "done":
            self.app.manager.remove(self.did)
        self.close()

    def _toggle(self, *_):
        rec = self.app.manager.get(self.did)
        if not rec:
            return
        if rec["status"] in ("running", "queued"):
            self.app.manager.pause(self.did)
        elif rec["status"] == "done":
            self.app.open_file(self.did)
        else:
            self.app.manager.resume(self.did)
        self._tick()

    def _set_conn_rows(self, rows):
        while len(self._conn_rows) < len(rows):
            r = ConnRow()
            self.conn_list.append(r)
            self._conn_rows.append(r)
        while len(self._conn_rows) > len(rows):
            r = self._conn_rows.pop()
            self.conn_list.remove(r.get_parent())
        for widget, values in zip(self._conn_rows, rows):
            widget.set(*values)

    def _tick(self):
        rec = self.app.manager.get(self.did)
        if not rec:
            self.close()
            return False
        snap = self.app.manager.snapshot(self.did) or {}
        status = rec["status"]
        name = rec.get("filename") or rec["url"].rsplit("/", 1)[-1] or rec["url"]
        self.name.set_label(name)
        self.name.set_tooltip_text(name)
        self.set_title(name)
        host = urlsplit(rec["url"]).hostname or ""
        self.host.set_label(host)
        icon = "video-x-generic-symbolic" if rec.get("kind") == "video" else Gio.content_type_get_symbolic_icon(
            Gio.content_type_guess(name, None)[0]).get_names()[0]
        self.icon.set_from_icon_name(icon)

        total = snap.get("total") or rec.get("total") or 0
        done = snap.get("downloaded") or rec.get("downloaded") or 0
        if status == "done" and total:
            done = total
        frac = done / total if total else (1.0 if status == "done" else 0.0)
        running = status == "running"
        speed = (snap.get("speed") or 0) if running else 0
        eta = snap.get("eta")
        if eta is None and total and speed:
            eta = (total - done) / speed

        if total or status == "done":
            self.pct.set_label(f"%{frac * 100:.0f}" if frac < 0.995 or status == "done" else "%99")
        else:
            self.pct.set_label(human_size(done))
        if running and snap.get("phase") == "birleştiriliyor":
            self.speed.set_label("Birleştiriliyor")
            self.eta.set_label("ffmpeg")
        elif running:
            self.speed.set_label(human_speed(speed) if speed else "Bağlanıyor…")
            self.eta.set_label(f"{human_eta(eta)} kaldı" if eta is not None and speed else "")
        elif status == "done":
            elapsed = rec.get("elapsed") or 0
            self.speed.set_label(human_size(total) if total else "")
            self.eta.set_label(f"{human_eta(elapsed)} sürdü" if elapsed else "")
        else:
            self.speed.set_label("")
            self.eta.set_label("")
        self.bar.update(total, snap.get("segments"), frac, done=(status == "done"))

        parts = [f"{human_size(done)} / {human_size(total)}" if total else human_size(done)]
        if running and rec["kind"] != "video" and snap.get("connections"):
            parts.append(f"{snap['connections']} bağlantı")
            live = [l for l in snap.get("lanes") or [] if not l["dead"]]
            if len(live) > 1:
                parts.append(f"{len(live)} bağdaştırıcı")
        self.amount.set_label(" · ".join(parts))

        st_text = STATUS_TEXT.get(status, status)
        set_chip(self.chip_status, st_text, STATUS_STYLE.get(status))
        self.chip_status.set_tooltip_text(rec.get("error") or None)
        vt = rec.get("vt") or {}
        vt_text, vt_level, vt_tip = vt_display(vt, status)
        set_chip(self.chip_vt, f"VT {vt_text}" if vt_text else "", VT_STYLE.get(vt_level))
        self.chip_vt.set_tooltip_text(vt_tip or None)

        if status in ("running", "queued"):
            self.toggle_btn.set_label("Duraklat")
            self.cancel_btn.set_label("İptal")
        elif status == "done":
            self.toggle_btn.set_label("Dosyayı aç")
            self.cancel_btn.set_label("Kapat")
        else:
            self.toggle_btn.set_label("Devam et" if status == "paused" else "Yeniden dene")
            self.cancel_btn.set_label("İptal")
        if status == "done":
            self.cancel_btn.remove_css_class("destructive-action")
        else:
            self.cancel_btn.add_css_class("destructive-action")

        hist = self.app.speed_history(self.did, recent=True)
        self.graph.set_samples(hist)
        if hist:
            avg = sum(hist) / len(hist)
            self.graph_note.set_label(f"son {len(hist) // 2} sn · ort. {human_speed(avg)}")
        else:
            self.graph_note.set_label("veri yok")

        if self.expanded:
            self._fill_detail(rec, snap, status)

        if status == "done" and self._last_status not in (None, "done") and self.app.settings.get("finish_popup"):
            GLib.idle_add(self.close)
        self._last_status = status
        return True

    def _fill_detail(self, rec, snap, status):
        self.row_path.set_subtitle(GLib.markup_escape_text(rec.get("path") or rec.get("directory") or "—"))
        self.row_url.set_subtitle(GLib.markup_escape_text(rec["url"]))
        if rec["kind"] == "video":
            self.row_resume.set_subtitle("yt-dlp · kaldığı yerden sürer")
        else:
            resumable = snap.get("resumable") if snap else rec.get("resumable")
            conn = f"{snap.get('connections', 0)} etkin / sınır {snap.get('allowed')}" if status == "running" and snap.get("allowed") else ""
            lanes = " · ".join(f"{l['name']} {'kopuk' if l['dead'] else human_speed(l['speed'])}"
                               for l in snap.get("lanes") or []) if status == "running" else ""
            self.row_resume.set_subtitle(" · ".join(x for x in (
                "Parçalı, kaldığı yerden sürer" if resumable else "Tek bağlantı, devam ettirilemez", conn, lanes) if x))
        vt = rec.get("vt") or {}
        sha = rec.get("sha256") or ""
        exp = vt.get("expected") or {}
        sha_text = sha or ("hesaplanıyor…" if vt.get("state") == "hashing" else "—")
        if sha and (exp.get("sha256") or exp.get("md5")):
            sha_text += "\n" + ("✓ sunucunun bildirdiği değerle aynı" if not vt.get("mismatch")
                                else "✕ sunucunun bildirdiği değerle uyuşmuyor")
        self.row_sha.set_subtitle(GLib.markup_escape_text(sha_text))
        post = rec.get("post") or {}
        ck = post.get("checksum")
        check_text = {
            "searching": "checksum dosyası aranıyor…",
            "ok": f"✓ {vt.get('checksum_file') or 'checksum'} ile doğrulandı",
            "bad": f"✕ {vt.get('checksum_file') or 'checksum'} ile UYUŞMUYOR",
            "none": "yayımlanmış checksum dosyası bulunamadı",
            "error": "doğrulanamadı",
        }.get(ck)
        self.row_check.set_visible(bool(check_text))
        self.row_check.set_subtitle(GLib.markup_escape_text(check_text or ""))
        self.row_check.set_tooltip_text(vt.get("checksum_src") or None)
        for c in ("success", "error"):
            self.row_check.remove_css_class(c)
        if ck in ("ok", "bad"):
            self.row_check.add_css_class("success" if ck == "ok" else "error")
        act = post.get("action")
        post_text = {
            "extracting": f"arşiv açılıyor… ({post.get('rule')})",
            "extracted": f"arşiv açıldı → {post.get('dest')}" + (" · arşiv silindi" if post.get("archive_deleted") else ""),
            "opened": f"dosya açıldı ({post.get('rule')})",
            "skipped": post.get("reason"),
            "error": post.get("reason"),
        }.get(act)
        self.row_post.set_visible(bool(post_text))
        self.row_post.set_subtitle(GLib.markup_escape_text(post_text or ""))
        self.post_btn.set_visible(act == "extracted")
        text, level, tip = vt_display(vt, status)
        if not text:
            tip = "Ayarlar ▸ Güvenlik bölümünden API anahtarı girin" if vt.get("state") in (None, "disabled") else "—"
        self.row_vt.set_subtitle(GLib.markup_escape_text(tip or text))
        for c in ("success", "warning", "error"):
            self.row_vt.remove_css_class(c)
        if VT_STYLE.get(level) in ("success", "warning", "error"):
            self.row_vt.add_css_class(VT_STYLE[level])
        self.vt_open.set_visible(bool(sha or vt.get("link") or vt.get("url_link")))
        self.vt_upload.set_visible(status == "done" and vt.get("state") == "unknown")
        self.vt_recheck.set_visible(status == "done")
        us = vt.get("url_stats") or {}
        if vt.get("url_verdict"):
            self.row_url_vt.set_subtitle(f"{vt.get('url_scope') or 'link'}: {us.get('malicious', 0)}/{us.get('total', 0)} zararlı")
        else:
            self.row_url_vt.set_subtitle({"checking": "kontrol ediliyor…", "unknown": "VirusTotal'da kayıtlı değil",
                                          "error": vt.get("error") or "hata"}.get(vt.get("url_state"), "—"))
        self._actions.lookup_action("copy-hash").set_enabled(bool(sha))
        self._actions.lookup_action("vt-open").set_enabled(bool(sha or vt.get("link") or vt.get("url_link")))

        rows = []
        if rec["kind"] == "video":
            self.conn_group.set_title("Akışlar")
            for p in snap.get("parts") or []:
                t = p.get("total") or 0
                rows.append((str(p["format"]), p["pos"] / t if t else 0.0,
                             f"{human_size(p['pos'])} / {human_size(t)}" if t else human_size(p["pos"]), ""))
        elif status != "done":
            self.conn_group.set_title("Bağlantılar")
            for i, s in enumerate(snap.get("segments") or [], start=1):
                end = s.get("end")
                size = (end - s["start"] + 1) if end is not None else None
                got = max(0, (min(s["pos"], end + 1) if end is not None else s["pos"]) - s["start"])
                st = s.get("state") or ""
                if s.get("lane") and s.get("active"):
                    st = f"{st} · {s['lane']}"
                rows.append((str(i), got / size if size else 0.0,
                             f"{human_size(got)} / {human_size(size)}" if size else human_size(got), st))
        self._set_conn_rows(rows)
        self.conn_group.set_visible(bool(rows))
