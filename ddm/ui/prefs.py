import os
import shutil
import threading

from gi.repository import Adw, Gio, GLib, Gtk

from ..config import DATA_DIR
from ..video import update_ytdlp, ytdlp_binary, ytdlp_version
from ..netlanes import list_uplinks
from .rules import RulesPage

HEIGHT_CHOICES = [(0, "En iyi"), (2160, "2160p (4K)"), (1440, "1440p"), (1080, "1080p"), (720, "720p"),
                  (480, "480p"), (360, "360p")]


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.s = app.settings
        self.set_search_enabled(False)
        self.set_title("Ayarlar")
        self.set_content_width(720)
        self._general()
        self.add(RulesPage(self, self.s))
        self._connection()
        self._video()
        self._security()
        self._power()
        self._browser()

    def _switch(self, group, key, title, subtitle=None, on_change=None):
        row = Adw.SwitchRow(title=title)
        if subtitle:
            row.set_subtitle(subtitle)
        row.set_active(bool(self.s.get(key)))

        def changed(r, *_):
            self.s.set(key, r.get_active())
            if on_change:
                on_change(r.get_active())

        row.connect("notify::active", changed)
        group.add(row)
        return row

    def _spin(self, group, key, title, lo, hi, step=1, subtitle=None):
        row = Adw.SpinRow.new_with_range(lo, hi, step)
        row.set_title(title)
        if subtitle:
            row.set_subtitle(subtitle)
        row.set_value(int(self.s.get(key) or 0))
        row.connect("notify::value", lambda r, *_: self.s.set(key, int(r.get_value())))
        group.add(row)
        return row

    def _general(self):
        page = Adw.PreferencesPage(title="Genel", icon_name="preferences-system-symbolic")
        self.add(page)
        g = Adw.PreferencesGroup(title="Kayıt yeri")
        page.add(g)
        self.dir_row = Adw.ActionRow(title="İndirme klasörü", subtitle=GLib.markup_escape_text(self.s.get("download_dir")))
        btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
        btn.add_css_class("flat")
        btn.connect("clicked", self._choose_dir)
        self.dir_row.add_suffix(btn)
        g.add(self.dir_row)
        self._switch(g, "category_subfolders", "Kategori alt klasörleri",
                     "Dosyaları Video, Müzik, Arşiv… klasörlerine ayırır")

        g = Adw.PreferencesGroup(title="Kuyruk")
        page.add(g)
        self._spin(g, "max_active", "Aynı anda indirme", 1, 10)
        self._switch(g, "auto_resume", "Açılışta yarım kalanları sürdür")

        g = Adw.PreferencesGroup(title="Davranış")
        page.add(g)
        self._switch(g, "run_in_background", "Pencere kapanınca arka planda çalış",
                     "Tarayıcı eklentisi uygulama penceresi kapalıyken de çalışır")
        self._switch(g, "autostart", "Oturum açılınca başlat", on_change=self.app.set_autostart)
        self._switch(g, "tray_icon", "Üst çubukta simge göster",
                     "Ubuntu'da hazır gelen AppIndicator desteğini kullanır")
        self._switch(g, "tray_label", "Simgenin yanında hızı göster")
        self._switch(g, "notifications", "Bildirimler")
        self._switch(g, "clipboard_watch", "Panodaki linkleri öner",
                     "Pencere öne geldiğinde panodaki indirilebilir linki sorar")
        self._switch(g, "detail_on_start", "İndirme başlayınca ayrıntı penceresini aç")
        self._switch(g, "finish_popup", "İndirme bitince özet penceresi göster",
                     "Boyut, süre, hız grafiği ve VirusTotal sonucu; Dosyayı aç / Klasörü aç")

    def _connection(self):
        page = Adw.PreferencesPage(title="Bağlantı", icon_name="network-transmit-receive-symbolic")
        self.add(page)
        g = Adw.PreferencesGroup(title="Hız")
        page.add(g)
        self._spin(g, "connections", "Varsayılan bağlantı sayısı", 1, 16,
                   subtitle="Dosya başına paralel bağlantı. Sunucu 429/503 dönerse otomatik azaltılır")
        self._spin(g, "speed_limit_kib", "Hız sınırı (KB/s)", 0, 1_000_000, 128, subtitle="0 = sınırsız")
        self.lanes_group = Adw.PreferencesGroup(
            title="Çoklu bağdaştırıcı",
            description="Büyük dosyaların bağlantılarını birden fazla internet hattına dağıtır (örn. ev Wi-Fi'si + "
                        "telefondan USB paylaşım). Aynı modeme bağlı Wi-Fi ve kablo hız kazandırmaz. yt-dlp videolarında çalışmaz.")
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Yeniden tara")
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda *_: self._fill_lanes())
        self.lanes_group.set_header_suffix(refresh)
        page.add(self.lanes_group)
        self._lane_rows = []
        self._fill_lanes()

        g = Adw.PreferencesGroup(title="Dayanıklılık")
        page.add(g)
        self._spin(g, "retries", "Yeniden deneme", 0, 50)
        self._spin(g, "timeout", "Zaman aşımı (sn)", 5, 300)
        g = Adw.PreferencesGroup(title="Kimlik")
        page.add(g)
        ua = Adw.EntryRow(title="User-Agent")
        ua.set_text(self.s.get("user_agent") or "")
        ua.set_show_apply_button(True)
        ua.connect("apply", lambda r: self.s.set("user_agent", r.get_text().strip()))
        g.add(ua)

    def _fill_lanes(self):
        for r in self._lane_rows:
            self.lanes_group.remove(r)
        self._lane_rows = []
        enable = Adw.SwitchRow(title="Birden fazla bağdaştırıcıyla indir")
        enable.set_active(bool(self.s.get("multi_iface")))
        enable.connect("notify::active", lambda r, *_: self.s.set("multi_iface", r.get_active()))
        self.lanes_group.add(enable)
        self._lane_rows.append(enable)
        ups = list_uplinks()
        chosen = set(self.s.get("multi_ifaces") or [])
        for u in ups:
            bits = [u["label"]]
            if u.get("addr"):
                bits.append(u["addr"])
            if u.get("metered"):
                bits.append("ölçülü bağlantı — kota harcar")
            row = Adw.SwitchRow(title=u["name"], subtitle=GLib.markup_escape_text(" · ".join(bits)))
            row.set_active(u["name"] in chosen)

            def toggled(r, _p, name=u["name"]):
                cur = [n for n in (self.s.get("multi_ifaces") or []) if n != name]
                if r.get_active():
                    cur.append(name)
                self.s.set("multi_ifaces", cur)

            row.connect("notify::active", toggled)
            self.lanes_group.add(row)
            self._lane_rows.append(row)
        if len(ups) < 2:
            info = Adw.ActionRow(title=f"Şu an internete çıkan {len(ups)} bağdaştırıcı var",
                                 subtitle="İkinci bir bağlantı (telefon USB paylaşımı, ikinci Wi-Fi adaptörü…) "
                                          "takınca burada görünür; yenile düğmesiyle tekrar tara.")
            self.lanes_group.add(info)
            self._lane_rows.append(info)
        spin = Adw.SpinRow.new_with_range(1, 100000, 10)
        spin.set_title("En küçük dosya boyutu (MB)")
        spin.set_subtitle("Bunun altındaki dosyalar tek hattan iner")
        spin.set_value(int(self.s.get("multi_iface_min_mb") or 50))
        spin.connect("notify::value", lambda r, *_: self.s.set("multi_iface_min_mb", int(r.get_value())))
        self.lanes_group.add(spin)
        self._lane_rows.append(spin)

    def _video(self):
        page = Adw.PreferencesPage(title="Video", icon_name="video-x-generic-symbolic")
        self.add(page)
        g = Adw.PreferencesGroup(title="yt-dlp", description="YouTube ve diğer sitelerden video indirme motoru")
        page.add(g)
        self.ytdlp_row = Adw.ActionRow(title="Sürüm", subtitle="kontrol ediliyor…")
        self.ytdlp_btn = Gtk.Button(label="Güncelle", valign=Gtk.Align.CENTER)
        self.ytdlp_btn.connect("clicked", self._update_ytdlp)
        self.ytdlp_progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER, visible=False)
        self.ytdlp_progress.set_size_request(90, -1)
        self.ytdlp_row.add_suffix(self.ytdlp_progress)
        self.ytdlp_row.add_suffix(self.ytdlp_btn)
        g.add(self.ytdlp_row)
        ff = shutil.which("ffmpeg")
        ff_row = Adw.ActionRow(title="ffmpeg", subtitle=ff or "bulunamadı — ayrı görüntü/ses akışları birleştirilemez "
                                                               "(sudo apt install ffmpeg)")
        g.add(ff_row)
        self._refresh_ytdlp()

        g = Adw.PreferencesGroup(title="Varsayılanlar")
        page.add(g)
        model = Gtk.StringList.new([label for _, label in HEIGHT_CHOICES])
        row = Adw.ComboRow(title="Azami çözünürlük", model=model)
        cur = int(self.s.get("video_max_height") or 0)
        row.set_selected(next((i for i, (h, _) in enumerate(HEIGHT_CHOICES) if h == cur), 3))
        row.connect("notify::selected", lambda r, *_: self.s.set("video_max_height", HEIGHT_CHOICES[r.get_selected()][0]))
        g.add(row)
        crow = Adw.ComboRow(title="Kapsayıcı", model=Gtk.StringList.new(["MP4", "MKV"]))
        crow.set_selected(0 if (self.s.get("video_container") or "mp4") == "mp4" else 1)
        crow.connect("notify::selected", lambda r, *_: self.s.set("video_container", "mp4" if r.get_selected() == 0 else "mkv"))
        g.add(crow)
        self._spin(g, "ytdlp_fragments", "Paralel parça (HLS/DASH)", 1, 16)
        self._switch(g, "video_own_folder", "Her videoyu kendi klasörüne indir",
                     "Video Adı/Video Adı.mp4 şeklinde kaydedilir; doğrudan inen .mp4/.mkv dosyaları için de geçerli. "
                     "Yalnız ses indirmeleri klasöre konmaz")

    def _refresh_ytdlp(self):
        def work():
            v = ytdlp_version(self.s)
            path = ytdlp_binary(self.s)
            GLib.idle_add(self._set_ytdlp, v, path)

        threading.Thread(target=work, daemon=True).start()

    def _set_ytdlp(self, version, path):
        if version:
            self.ytdlp_row.set_subtitle(GLib.markup_escape_text(f"{version} · {path}"))
            self.ytdlp_btn.set_label("Güncelle")
        else:
            self.ytdlp_row.set_subtitle("Kurulu değil")
            self.ytdlp_btn.set_label("İndir")
        return False

    def _update_ytdlp(self, *_):
        self.ytdlp_btn.set_sensitive(False)
        self.ytdlp_progress.set_visible(True)
        self.ytdlp_progress.set_fraction(0)

        def prog(f):
            GLib.idle_add(self.ytdlp_progress.set_fraction, f)

        def work():
            try:
                update_ytdlp(prog)
                err = None
            except Exception as e:
                err = str(e)
            GLib.idle_add(done, err)

        def done(err):
            self.ytdlp_btn.set_sensitive(True)
            self.ytdlp_progress.set_visible(False)
            if err:
                self.add_toast(Adw.Toast(title=f"yt-dlp indirilemedi: {err[:120]}"))
            else:
                self.add_toast(Adw.Toast(title="yt-dlp güncellendi"))
            self._refresh_ytdlp()
            return False

        threading.Thread(target=work, daemon=True).start()

    def _power(self):
        page = Adw.PreferencesPage(title="Güç ve zamanlama", icon_name="preferences-system-time-symbolic")
        self.add(page)
        g = Adw.PreferencesGroup(title="Güç")
        page.add(g)
        self._switch(g, "inhibit_sleep", "İndirme sürerken uyku moduna geçme",
                     "Bilgisayar boşta kalsa da indirme bitene kadar uyumaz; ekran yine kararabilir")
        g.add(Adw.ActionRow(title="Tümü bitince uyku / kapatma",
                            subtitle="Ana menü ▸ Tümü bitince. Tek seferliktir; 60 sn geri sayımla iptal edilebilir"))

        g = Adw.PreferencesGroup(title="Zamanlayıcı",
                                 description="“Zamanla” denilen indirmeler yalnızca bu saatler arasında çalışır. "
                                             "Gece yarısını aşan aralıklar (23:00–06:00) da olur.")
        page.add(g)
        for key, title in (("sched_start", "Başlangıç"), ("sched_end", "Bitiş")):
            row = Adw.EntryRow(title=f"{title} (SS:DD)")
            row.set_text(self.s.get(key) or "")
            row.set_show_apply_button(True)
            row.set_input_purpose(Gtk.InputPurpose.DIGITS)

            def apply(r, k=key):
                txt = r.get_text().strip()
                parts = txt.split(":")
                ok = len(parts) == 2 and all(p.isdigit() for p in parts) and int(parts[0]) < 24 and int(parts[1]) < 60
                if ok:
                    r.remove_css_class("error")
                    self.s.set(k, f"{int(parts[0]):02d}:{int(parts[1]):02d}")
                else:
                    r.add_css_class("error")

            row.connect("apply", apply)
            g.add(row)

        g = Adw.PreferencesGroup(title="Ölçülü bağlantı",
                                 description="Telefondan internet paylaşımı gibi kotalı bağlantılarda ne yapılsın")
        page.add(g)
        actions = [("none", "Bir şey yapma"), ("pause", "İndirmeleri beklet"), ("limit", "Hızı sınırla")]
        combo = Adw.ComboRow(title="Davranış", model=Gtk.StringList.new([a[1] for a in actions]))
        cur = self.s.get("metered_action") or "pause"
        combo.set_selected(next((i for i, a in enumerate(actions) if a[0] == cur), 1))
        combo.connect("notify::selected", lambda r, *_: self.s.set("metered_action", actions[r.get_selected()][0]))
        g.add(combo)
        self._spin(g, "metered_limit_kib", "Sınır (KB/s)", 16, 100000, 16)

    def _security(self):
        page = Adw.PreferencesPage(title="Güvenlik", icon_name="security-high-symbolic")
        self.add(page)
        g = Adw.PreferencesGroup(
            title="VirusTotal",
            description="Dosyanın kendisi gönderilmez: indirme bitince SHA-256 değeri, başlarken de link adresi "
                        "sorgulanır. Ücretsiz anahtar: dakikada 4, günde 500 sorgu.")
        page.add(g)
        key = Adw.PasswordEntryRow(title="API anahtarı")
        key.set_text(self.s.get("vt_api_key") or "")
        key.set_show_apply_button(True)
        key.connect("apply", lambda r: self.s.set("vt_api_key", r.get_text().strip()))
        test = Gtk.Button(label="Dene", valign=Gtk.Align.CENTER)
        test.add_css_class("flat")
        test.connect("clicked", lambda b: self._test_vt(b, key.get_text()))
        key.add_suffix(test)
        g.add(key)
        link_row = Adw.ActionRow(title="Anahtarın yok mu?",
                                 subtitle="VirusTotal'da ücretsiz hesap aç, profil ▸ API Key bölümünden kopyala")
        link_row.add_suffix(Gtk.LinkButton(uri="https://www.virustotal.com/gui/join-us", label="Hesap aç",
                                           valign=Gtk.Align.CENTER))
        g.add(link_row)
        self._switch(g, "vt_enabled", "İndirilen dosyaları kontrol et",
                     "Her indirme bitince VirusTotal sonucu listede gösterilir; zararlıysa uyarılırsın")
        cg = Adw.PreferencesGroup(title="Bütünlük")
        self._switch(cg, "checksum_auto", "Checksum dosyalarını bul ve doğrula",
                     "İndirme bitince aynı dizinde SHA256SUMS ya da dosya.sha256 aranır; varsa dosya otomatik doğrulanır")
        page.add(cg)
        self._switch(g, "vt_url_check", "Linki indirme başlamadan kontrol et",
                     "Link ya da alan adı zararlı işaretliyse indirme duraklatılıp sorulur. Link adresi VirusTotal'a gider")

    def _test_vt(self, button, key):
        key = (key or "").strip()
        if not key:
            self.add_toast(Adw.Toast(title="Önce anahtarı girin"))
            return
        button.set_sensitive(False)

        def work():
            try:
                ok = self.app.manager.vt.test_key(key)
                msg = "Anahtar çalışıyor ✓" if ok else "Beklenmeyen yanıt"
            except Exception as e:
                msg = str(e) or "Bağlantı hatası"
            GLib.idle_add(done, msg)

        def done(msg):
            button.set_sensitive(True)
            self.add_toast(Adw.Toast(title=msg))
            return False

        threading.Thread(target=work, daemon=True).start()

    def _browser(self):
        page = Adw.PreferencesPage(title="Tarayıcı", icon_name="web-browser-symbolic")
        self.add(page)
        g = Adw.PreferencesGroup(title="Köprü")
        page.add(g)
        b = self.app.bridge
        status = f"127.0.0.1:{b.port} dinleniyor" if b and b.server else f"Başlatılamadı: {b.error if b else '—'}"
        g.add(Adw.ActionRow(title="Durum", subtitle=GLib.markup_escape_text(status)))
        self._spin(g, "bridge_port", "Port", 1024, 65535, subtitle="Değişiklik yeniden başlatınca geçerli olur; "
                                                                   "eklenti ayarlarındaki portla aynı olmalı")
        self._switch(g, "confirm_browser", "Tarayıcıdan gelenleri onayla",
                     "Kapalıysa indirme pencere açılmadan hemen başlar")

        g = Adw.PreferencesGroup(title="Yakalama",
                                 description="Firefox'ta başlayan indirmelerden hangilerinin DDM'ye devredileceği")
        page.add(g)
        exts = Adw.EntryRow(title="Dosya uzantıları (virgülle)")
        exts.set_text(", ".join(self.s.get("capture_exts") or []))
        exts.set_show_apply_button(True)
        exts.connect("apply", lambda r: self.s.set(
            "capture_exts", [e.strip().lower().lstrip(".") for e in r.get_text().split(",") if e.strip()]))
        g.add(exts)
        self._spin(g, "capture_min_kib", "En küçük boyut (KB)", 0, 10_000_000, 64, subtitle="0 = boyut sınırı yok")

        g = Adw.PreferencesGroup(title="Eklenti")
        page.add(g)
        ext_dir = DATA_DIR / "extension"
        row = Adw.ActionRow(title="Firefox eklentisi",
                            subtitle=GLib.markup_escape_text(str(ext_dir)))
        btn = Gtk.Button(label="Klasörü aç", valign=Gtk.Align.CENTER)
        btn.connect("clicked", lambda *_: Gtk.FileLauncher.new(Gio.File.new_for_path(str(ext_dir))).launch(
            self.get_root(), None, None) if os.path.isdir(ext_dir) else None)
        row.add_suffix(btn)
        g.add(row)

    def _choose_dir(self, *_):
        dialog = Gtk.FileDialog(title="İndirme klasörü")
        cur = self.s.get("download_dir")
        if os.path.isdir(cur):
            dialog.set_initial_folder(Gio.File.new_for_path(cur))

        def done(dlg, res):
            try:
                f = dlg.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.s.set("download_dir", f.get_path())
                self.dir_row.set_subtitle(GLib.markup_escape_text(f.get_path()))

        dialog.select_folder(self.get_root(), None, done)
