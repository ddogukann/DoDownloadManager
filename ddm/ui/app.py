import json
import os
import sys
from collections import deque
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import APP_ID, APP_NAME, VERSION
from ..bridge import Bridge, valid_url
from ..config import CONFIG_DIR, DATA_DIR, Settings, bridge_token, ensure_dirs
from ..manager import DownloadManager
from ..tray import TrayIcon
from ..util import filename_from_url, human_size
from ..virustotal import file_link
from .add_dialog import AddDialog
from .complete import CompleteWindow
from .detail import DetailWindow
from .prefs import PreferencesDialog
from .window import MainWindow

CSS = """
columnview.data-table progressbar trough { min-height: 6px; }
columnview.data-table progressbar progress { min-height: 6px; }
columnview.data-table progressbar text { font-size: 0.85em; font-feature-settings: "tnum"; }
columnview.data-table > listview > row { min-height: 38px; }
.ddm-hero { font-size: 44px; font-weight: 800; letter-spacing: -1px; font-feature-settings: "tnum"; }
.ddm-speed { font-size: 20px; font-weight: 700; font-feature-settings: "tnum"; }
.ddm-chip { padding: 3px 10px; border-radius: 999px; font-size: 0.82em; font-weight: 700;
            background: alpha(currentColor, 0.08); }
.ddm-chip.success { color: @success_color; background: alpha(@success_color, 0.14); }
.ddm-chip.warning { color: @warning_color; background: alpha(@warning_color, 0.16); }
.ddm-chip.error { color: @error_color; background: alpha(@error_color, 0.14); }
.ddm-chip.accent { color: @accent_color; background: alpha(@accent_color, 0.14); }
.ddm-chip.dim { color: alpha(currentColor, 0.65); }
.ddm-tile { padding: 10px 14px; border-radius: 12px; background: alpha(currentColor, 0.05); }
.ddm-tile-value { font-size: 17px; font-weight: 700; font-feature-settings: "tnum"; }
.ddm-tile-caption { font-size: 0.8em; opacity: 0.65; }
.ddm-card-pad { padding: 12px 14px; }
.ddm-graph-frame { padding: 6px; border-radius: 12px; background: alpha(currentColor, 0.04); }
.ddm-done-badge { min-width: 56px; min-height: 56px; border-radius: 999px; color: white;
                  background: @success_bg_color; margin-top: 4px; }
.ddm-file-icon { min-width: 48px; min-height: 48px; border-radius: 12px; background: alpha(@accent_bg_color, 0.14);
                 color: @accent_color; }
progressbar.ddm-thin trough, progressbar.ddm-thin progress { min-height: 6px; border-radius: 99px; }
"""


class DDMApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.window = None
        self.settings = None
        self.manager = None
        self.bridge = None
        self.tray = None
        self._ticks = 0
        self.detail_windows = {}
        self.complete_windows = {}
        self._sent = set()
        self._inhibit = 0
        self._was_active = False
        self.after_all = "none"
        self._countdown = None
        self._metered_override = False
        self._hist = {}
        self._held = False
        self._bg_notice_shown = False
        self.add_main_option("background", ord("b"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Pencere açmadan arka planda başlat", None)
        self.add_main_option("video", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Verilen linkleri video olarak (yt-dlp) indir", None)
        self.add_main_option("now", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Onay penceresi göstermeden kuyruğa ekle", None)
        self.add_main_option("new", ord("n"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Yeni indirme penceresini aç", None)
        self.add_main_option("quit", ord("q"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Çalışan örneği kapat", None)

    def do_startup(self):
        Adw.Application.do_startup(self)
        ensure_dirs()
        GLib.set_application_name(APP_NAME)
        Gtk.Window.set_default_icon_name(APP_ID)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.settings = Settings()
        self.manager = DownloadManager(self.settings)
        self.manager.connect(lambda kind, rec: GLib.idle_add(self._on_manager_event, kind, rec))
        self.bridge = Bridge(self.manager, self.settings, bridge_token(), self._bridge_download)
        self.bridge.start()
        self._install_actions()
        self.tray = TrayIcon(self)
        if self.settings.get("tray_icon"):
            self.tray.start()
        if self.settings.get("run_in_background"):
            self.hold()
            self._held = True
        self.settings.connect(self._on_setting)
        GLib.timeout_add(500, self._tick)
        GLib.timeout_add_seconds(30, self._sched_tick)
        self.netmon = Gio.NetworkMonitor.get_default()
        self.netmon.connect("notify::network-metered", lambda *_: GLib.idle_add(self._apply_metered, True))
        GLib.idle_add(self._apply_metered, False)
        self.manager.schedule()
        GLib.timeout_add(1500, self._withdraw_stale)

    def do_shutdown(self):
        if self.tray:
            self.tray.stop()
        if self.manager:
            self.manager.shutdown()
        if self.bridge:
            self.bridge.stop()
        Adw.Application.do_shutdown(self)

    def do_activate(self):
        self.present_window()

    def do_command_line(self, cmd):
        opts = cmd.get_options_dict().end().unpack()
        if opts.get("quit"):
            self.quit_app()
            return 0
        urls = [a for a in cmd.get_arguments()[1:] if not a.startswith("-") and valid_url(a)]
        first_window = self.window is None
        if opts.get("background") and first_window and not urls:
            return 0
        self.present_window()
        kind = "video" if opts.get("video") else None
        if urls and (opts.get("now") or len(urls) > 1):
            for u in urls:
                if kind == "video":
                    self.manager.add(u, kind="video", options=self._video_defaults())
                else:
                    self.manager.add(u)
            self.window.toast(f"{len(urls)} link kuyruğa eklendi")
        elif urls:
            self.show_add({"url": urls[0], "kind": kind})
        elif opts.get("new"):
            self.show_add({})
        return 0

    def _sched_tick(self):
        self.manager.enforce_schedule()
        return True

    def _apply_metered(self, changed=False):
        metered = bool(self.netmon.get_network_metered())
        if changed and not metered:
            self._metered_override = False
        action = self.settings.get("metered_action") or "pause"
        hold = metered and action == "pause" and not self._metered_override
        newly = self.manager.set_hold("metered", hold)
        self.manager.set_metered_limit(self.settings.get("metered_limit_kib") if metered and action == "limit" else 0)
        if newly and hold and self.window is not None:
            t = Adw.Toast(title="Ölçülü bağlantı: indirmeler bekletiliyor", button_label="Yine de indir", timeout=8)
            t.connect("button-clicked", lambda *_: self._override_metered())
            self.window.toasts.add_toast(t)
        return False

    def _override_metered(self):
        self._metered_override = True
        self._apply_metered()

    def _set_after_all(self, action, value):
        action.set_state(value)
        self.after_all = value.get_string()
        if self.window is not None:
            self.window.set_after_indicator(self.after_all)
            names = {"suspend": "uyku moduna geçilecek", "poweroff": "bilgisayar kapatılacak"}
            if self.after_all in names:
                self.window.toast(f"Tüm indirmeler bitince {names[self.after_all]}")

    def _power_check(self, active, running):
        if self.settings.get("inhibit_sleep") and running and not self._inhibit:
            self._inhibit = self.inhibit(None, Gtk.ApplicationInhibitFlags.SUSPEND, "DDM dosya indiriyor")
        elif (not running or not self.settings.get("inhibit_sleep")) and self._inhibit:
            self.uninhibit(self._inhibit)
            self._inhibit = 0
        if self._was_active and not active and self.after_all != "none" and self._countdown is None:
            self._start_countdown(self.after_all)
        self._was_active = active

    def _start_countdown(self, mode):
        self.present_window()
        verb = "uyku moduna geçecek" if mode == "suspend" else "kapanacak"
        dlg = Adw.AlertDialog(heading="Tüm indirmeler bitti", body=f"Bilgisayar 60 saniye içinde {verb}.")
        dlg.add_response("cancel", "İptal")
        dlg.add_response("now", "Şimdi")
        dlg.set_response_appearance("now", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_close_response("cancel")
        state = {"left": 60, "dlg": dlg, "mode": mode, "src": 0}

        def tick():
            state["left"] -= 1
            if state["left"] <= 0:
                state["src"] = 0
                dlg.force_close()
                self._do_power(mode)
                return False
            dlg.set_body(f"Bilgisayar {state['left']} saniye içinde {verb}.")
            return True

        def resp(d, r):
            if state["src"]:
                GLib.source_remove(state["src"])
                state["src"] = 0
            self._countdown = None
            if r == "now":
                self._do_power(mode)
            elif r == "cancel":
                self.window.toast("İptal edildi")

        dlg.connect("response", resp)
        state["src"] = GLib.timeout_add_seconds(1, tick)
        self._countdown = state
        dlg.present(self.window)

    def _do_power(self, mode):
        self._countdown = None
        act = self.lookup_action("after-all")
        if act:
            self._set_after_all(act, GLib.Variant.new_string("none"))
        method = "Suspend" if mode == "suspend" else "PowerOff"
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            bus.call_sync("org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
                          method, GLib.Variant("(b)", (True,)), None, Gio.DBusCallFlags.NONE, 10000, None)
        except GLib.Error as e:
            if self.window is not None:
                self.window.toast(f"İşlem yapılamadı: {e.message}")

    def _on_setting(self, key, value):
        if key in ("metered_action", "metered_limit_kib"):
            GLib.idle_add(self._apply_metered, False)
        if key == "run_in_background":
            if value and not self._held:
                self.hold()
                self._held = True
            elif not value and self._held:
                self.release()
                self._held = False
        elif key == "tray_icon":
            if value:
                self.tray.start()
                self.tray.update(self.manager.progress(), self.settings.get("tray_label"))
            else:
                self.tray.stop()
        elif key == "tray_label" and self.settings.get("tray_icon"):
            self.tray.update(self.manager.progress(), bool(value))

    def ensure_window(self):
        if self.window is None:
            self.window = MainWindow(self)
            self.window.connect("close-request", self._on_close_request)
        return self.window

    def present_window(self):
        w = self.ensure_window()
        if not getattr(w, "_notif_hooked", False):
            w._notif_hooked = True
            w.connect("notify::is-active", lambda win, _p: self.withdraw_all() if win.is_active() else None)
        w.present()

    def _notif_file(self):
        return DATA_DIR / "notifications.json"

    def _load_sent(self):
        try:
            data = json.loads(self._notif_file().read_text(encoding="utf-8"))
            return set(data) if isinstance(data, list) else set()
        except (OSError, ValueError):
            return set()

    def _save_sent(self):
        try:
            self._notif_file().write_text(json.dumps(sorted(self._sent)), encoding="utf-8")
        except OSError:
            pass

    def withdraw(self, did):
        changed = False
        for nid in (f"dl-{did}", f"vt-{did}"):
            if nid in self._sent:
                self.withdraw_notification(nid)
                self._sent.discard(nid)
                changed = True
        if changed:
            self._save_sent()

    def withdraw_all(self):
        if not self._sent:
            return
        for nid in list(self._sent):
            self.withdraw_notification(nid)
        self._sent.clear()
        self._save_sent()

    def _withdraw_stale(self):
        ids = {"bg"} | self._load_sent()
        for r in self.manager.records()[-300:]:
            ids.add(f"dl-{r['id']}")
            ids.add(f"vt-{r['id']}")
        for nid in ids:
            self.withdraw_notification(nid)
        self._sent.clear()
        self._save_sent()
        return False

    def _on_close_request(self, win):
        if self.settings.get("run_in_background"):
            win.set_visible(False)
            if not self._bg_notice_shown and self.manager.active_count():
                self._bg_notice_shown = True
                where = "üst çubuktaki DDM simgesinden" if self.settings.get("tray_icon") else "uygulama menüsünden"
                self._notify("bg", "DDM arka planda çalışıyor",
                             f"İndirmeler sürüyor. Pencereyi {where} yeniden açabilirsiniz.")
            return True
        if self.manager.active_count():
            dlg = Adw.AlertDialog(heading="Etkin indirmeler var",
                                  body="Çıkarsanız indirmeler duraklatılır ve sonraki açılışta sürer.")
            dlg.add_response("cancel", "Vazgeç")
            dlg.add_response("quit", "Çık")
            dlg.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.connect("response", lambda d, r: self.quit_app() if r == "quit" else None)
            dlg.present(win)
            return True
        self.quit_app()
        return True

    def quit_app(self):
        for w in list(self.detail_windows.values()):
            w.close()
        if self._held:
            self.release()
            self._held = False
        self.quit()

    def _install_actions(self):
        simple = {
            "add": lambda *_: self.show_add({}),
            "preferences": lambda *_: self.show_preferences(),
            "about": lambda *_: self.show_about(),
            "quit": lambda *_: self.quit_app(),
            "show-window": lambda *_: self.present_window(),
            "pause-all": lambda *_: self.manager.pause_all(),
            "resume-all": lambda *_: self.manager.resume_all(),
            "clear-finished": lambda *_: self.manager.clear_finished(),
        }
        for name, cb in simple.items():
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
        after = Gio.SimpleAction.new_stateful("after-all", GLib.VariantType.new("s"), GLib.Variant.new_string("none"))
        after.connect("change-state", self._set_after_all)
        self.add_action(after)
        for name, cb in {"open-file": self.open_file, "open-folder": self.open_folder,
                         "show-details": self._show_details_from_action}.items():
            a = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            a.connect("activate", lambda act, param, f=cb: f(param.get_string()))
            self.add_action(a)
        self.set_accels_for_action("app.add", ["<Control>n"])
        self.set_accels_for_action("app.preferences", ["<Control>comma"])
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("window.close", ["<Control>w"])

    def _tick(self):
        progress = self.manager.progress()
        if self.window is not None and self.window.get_visible():
            self.window.tick(progress)
        for did, snap in progress.items():
            h = self._hist.get(did)
            if h is None:
                h = self._hist[did] = {"recent": deque(maxlen=120), "all": [], "peak": 0.0, "max_conns": 0}
            spd = float(snap.get("speed") or 0)
            h["recent"].append(spd)
            h["all"].append(spd)
            if len(h["all"]) > 480:
                h["all"] = [(h["all"][i] + h["all"][i + 1]) / 2 for i in range(0, len(h["all"]) - 1, 2)]
            h["peak"] = max(h["peak"], spd)
            h["max_conns"] = max(h["max_conns"], int(snap.get("connections") or 0))
        running = bool(progress)
        self._power_check(self.manager.active_count() > 0, running)
        self._ticks += 1
        if self.tray is not None and self.tray.conn is not None and self._ticks % 2 == 0:
            self.tray.update(progress, self.settings.get("tray_label"))
        return True

    def speed_history(self, did, recent=False):
        h = self._hist.get(did)
        if not h:
            return []
        return list(h["recent"]) if recent else list(h["all"])

    def _show_complete(self, rec):
        if rec["id"] in self.complete_windows:
            self.complete_windows[rec["id"]].present()
            return True
        if len(self.complete_windows) >= 3:
            return False
        stats = self._hist.get(rec["id"]) or {}
        w = CompleteWindow(self, rec["id"], {"peak": stats.get("peak"), "max_conns": stats.get("max_conns")})
        self.complete_windows[rec["id"]] = w
        w.present()
        return True

    def _on_manager_event(self, kind, rec):
        if kind == "vt_alert":
            self._vt_alert(rec)
            return False
        if kind == "post_open":
            self.open_file(rec["id"])
            return False
        if kind == "post_open_path":
            if rec.get("_path"):
                self._launch(rec["_path"])
            return False
        if self.window is not None:
            self.window.on_manager_event(kind, rec)
        if kind == "removed":
            self._hist.pop(rec["id"], None)
            self.withdraw(rec["id"])
        if kind == "finished":
            win_active = self.window is not None and self.window.is_active()
            if self.settings.get("finish_popup") and self._show_complete(rec):
                pass
            elif not win_active:
                self._notify(f"dl-{rec['id']}", "İndirme tamamlandı", rec.get("filename") or rec["url"],
                             default=("app.open-file", rec["id"]),
                             buttons=[("Klasörü aç", "app.open-folder", rec["id"])])
            elif self.window:
                t = Adw.Toast(title=f"Tamamlandı: {rec.get('filename') or ''}", button_label="Aç", timeout=4)
                t.connect("button-clicked", lambda *_: self.open_file(rec["id"]))
                self.window.toasts.add_toast(t)
        elif kind == "failed":
            if self.window is None or not self.window.is_active():
                self._notify(f"dl-{rec['id']}", "İndirme başarısız",
                             f"{rec.get('filename') or rec['url']}\n{rec.get('error') or ''}",
                             default=("app.show-details", rec["id"]))
        return False

    def _vt_alert(self, rec):
        vt = rec.get("vt") or {}
        name = rec.get("filename") or rec["url"]
        if rec.get("_alert") == "checksum":
            body = f"{name}\n{vt.get('checksum_file') or 'Checksum'} dosyasındaki değerle uyuşmuyor; dosya bozuk ya da değiştirilmiş olabilir."
            self._notify(f"vt-{rec['id']}", "Dosya doğrulanamadı", body, default=("app.show-details", rec["id"]),
                         force=True)
            if self.window is not None and self.window.get_visible():
                t = Adw.Toast(title=f"Checksum uyuşmuyor: {name}", button_label="Ayrıntı", timeout=8)
                t.connect("button-clicked", lambda *_: self.show_details(rec["id"]))
                self.window.toasts.add_toast(t)
            return
        if rec.get("_alert") == "url":
            us = vt.get("url_stats") or {}
            self.present_window()
            dlg = Adw.AlertDialog(
                heading="Bu link zararlı olarak işaretlenmiş",
                body=f"{name}\n\nVirusTotal'da {us.get('malicious', 0)}/{us.get('total', 0)} güvenlik motoru bu "
                     f"{vt.get('url_scope') or 'linki'} zararlı buldu. İndirme duraklatıldı.")
            dlg.add_response("cancel", "İndirmeyi iptal et")
            dlg.add_response("show", "VirusTotal'da gör")
            dlg.add_response("continue", "Yine de indir")
            dlg.set_response_appearance("cancel", Adw.ResponseAppearance.SUGGESTED)
            dlg.set_response_appearance("continue", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_default_response("cancel")
            dlg.set_close_response("dismiss")

            def resp(d, r):
                if r == "cancel":
                    self.manager.remove(rec["id"])
                elif r == "continue":
                    self.manager.vt_acknowledge(rec["id"])
                elif r == "show" and vt.get("url_link"):
                    self.open_uri(vt["url_link"])

            dlg.connect("response", resp)
            dlg.present(self.window)
            return
        stats = vt.get("stats") or {}
        body = f"{name}\nVirusTotal: {stats.get('malicious', 0)}/{stats.get('total', 0)} motor zararlı buldu."
        self._notify(f"vt-{rec['id']}", "Zararlı olabilecek dosya", body, default=("app.show-details", rec["id"]),
                     force=True)
        if self.window is not None and self.window.get_visible():
            t = Adw.Toast(title=f"Zararlı olabilir: {name}", button_label="Ayrıntı", timeout=8)
            t.connect("button-clicked", lambda *_: self.show_details(rec["id"]))
            self.window.toasts.add_toast(t)

    def open_uri(self, uri):
        Gtk.UriLauncher.new(uri).launch(self.window, None, None)

    def vt_open(self, did):
        rec = self.manager.get(did)
        if not rec:
            return
        vt = rec.get("vt") or {}
        link = vt.get("link") or (file_link(rec["sha256"]) if rec.get("sha256") else None) or vt.get("url_link")
        if link:
            self.open_uri(link)

    def vt_upload(self, did):
        rec = self.manager.get(did)
        if not rec:
            return
        if not self.manager.vt.enabled():
            if self.window:
                self.window.toast("Önce Ayarlar ▸ Güvenlik bölümünden VirusTotal API anahtarı girin")
            return
        dlg = Adw.AlertDialog(
            heading="Dosya VirusTotal'a yüklensin mi?",
            body=f"{rec.get('filename')}\n\nYüklenen dosyalar VirusTotal'ın güvenlik ortaklarıyla paylaşılır ve "
                 "aranabilir hale gelir. Kişisel ya da gizli belgeleri yüklemeyin. 650 MB sınırı vardır.")
        dlg.add_response("cancel", "Vazgeç")
        dlg.add_response("upload", "Yükle ve tara")
        dlg.set_response_appearance("upload", Adw.ResponseAppearance.SUGGESTED)
        dlg.connect("response", lambda d, r: self.manager.vt_upload(did) if r == "upload" else None)
        dlg.present(self.window)

    def _notify(self, nid, title, body, default=None, buttons=(), force=False):
        if not self.settings.get("notifications") and not force:
            return
        n = Gio.Notification.new(title)
        n.set_body(body)
        n.set_icon(Gio.ThemedIcon.new(APP_ID))
        if default:
            n.set_default_action_and_target(default[0], GLib.Variant.new_string(default[1]))
        else:
            n.set_default_action("app.show-window")
        for label, action, target in buttons:
            n.add_button_with_target(label, action, GLib.Variant.new_string(target))
        self.send_notification(nid, n)
        self._sent.add(nid)
        self._save_sent()

    def _video_defaults(self):
        return {"height": int(self.settings.get("video_max_height") or 0),
                "container": self.settings.get("video_container") or "mp4"}

    def _bridge_download(self, req):
        if req["kind"] == "file":
            name = req.get("filename") or filename_from_url(req["url"])
            cand = self.manager.find_resume_candidate(name, req.get("size"))
            if cand:
                GLib.idle_add(self._offer_resume, cand, req)
                return {"pending": True}
        if self.settings.get("confirm_browser"):
            GLib.idle_add(self._show_add_idle, req)
            return {"pending": True}
        headers = req.get("headers") or {}
        if req["kind"] == "video":
            opts = self._video_defaults()
            opts["title"] = req.get("title")
            if req.get("cookies"):
                opts["cookies"] = req["cookies"]
            rec = self.manager.add(req["url"], kind="video", headers=headers, options=opts, page_url=req.get("page_url"))
        else:
            rec = self.manager.add(req["url"], filename=req.get("filename"), headers=headers,
                                   page_url=req.get("page_url"))
        return {"id": rec["id"], "queued": True}

    def _offer_resume(self, cand, req):
        self.present_window()
        pct = int((cand.get("downloaded") or 0) * 100 / cand["total"]) if cand.get("total") else 0
        expired = (cand.get("options") or {}).get("expired")
        dlg = Adw.AlertDialog(
            heading="Yarım kalan indirme bulundu",
            body=f"{cand.get('filename')}\n\n%{pct} indirilmişti ({human_size(cand.get('downloaded'))}"
                 f"{' / ' + human_size(cand['total']) if cand.get('total') else ''})."
                 f"{' Eski linkin süresi dolmuştu.' if expired else ''} Bu yeni linkle kaldığı yerden devam edilsin mi?")
        dlg.add_response("new", "Yeni indirme olarak ekle")
        dlg.add_response("resume", "Kaldığı yerden sürdür")
        dlg.set_response_appearance("resume", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("resume")
        dlg.set_close_response("dismiss")

        def resp(d, r):
            if r == "resume":
                self.manager.refresh_url(cand["id"], req["url"], req.get("headers") or {}, req.get("page_url"))
                if self.window:
                    self.window.toast(f"{cand.get('filename')} yeni linkle kaldığı yerden sürüyor")
            elif r == "new":
                if self.settings.get("confirm_browser"):
                    self.show_add(req)
                else:
                    self.manager.add(req["url"], filename=req.get("filename"), headers=req.get("headers") or {},
                                     page_url=req.get("page_url"))

        dlg.connect("response", resp)
        dlg.present(self.window)
        return False

    def refresh_link_dialog(self, did):
        rec = self.manager.get(did)
        if not rec:
            return
        self.present_window()
        entry = Gtk.Entry(placeholder_text="https://… yeni indirme linki", hexpand=True, activates_default=True)
        body = (f"{rec.get('filename')}\n\nYeni linki yapıştır. Ya da indirme sayfasını yeniden açıp tarayıcıda "
                "indir'e bas; DDM aynı dosyayı tanıyıp kaldığı yerden sürdürmeyi önerir.")
        dlg = Adw.AlertDialog(heading="Linki yenile", body=body, extra_child=entry)
        dlg.add_response("cancel", "Vazgeç")
        if rec.get("page_url"):
            dlg.add_response("page", "Sayfayı aç")
        dlg.add_response("apply", "Sürdür")
        dlg.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("apply")

        def clip_done(c, res):
            try:
                text = (c.read_text_finish(res) or "").strip()
            except GLib.Error:
                return
            if valid_url(text) and text != rec["url"] and not entry.get_text():
                entry.set_text(text)

        self.window.get_clipboard().read_text_async(None, clip_done)

        def resp(d, r):
            if r == "apply":
                url = entry.get_text().strip()
                if valid_url(url):
                    self.manager.refresh_url(did, url)
                elif self.window:
                    self.window.toast("Geçerli bir link girin")
            elif r == "page":
                self.open_uri(rec["page_url"])

        dlg.connect("response", resp)
        dlg.present(self.window)

    def _show_add_idle(self, req):
        self.show_add(req)
        return False

    def show_add(self, request):
        self.present_window()
        AddDialog(self, request).present(self.window)

    def show_preferences(self):
        self.present_window()
        PreferencesDialog(self).present(self.window)

    def _show_details_from_action(self, did):
        self.withdraw(did)
        self.present_window()
        self.show_details(did)

    def show_details(self, did):
        if not self.manager.get(did):
            return
        w = self.detail_windows.get(did)
        if w is None:
            w = DetailWindow(self, did)
            if self.window is not None and self.window.get_visible():
                w.set_transient_for(self.window)
            self.detail_windows[did] = w
        w.present()

    def show_about(self):
        about = Adw.AboutDialog(application_name=APP_NAME, application_icon=APP_ID, version=VERSION,
                                developer_name="Doğukan",
                                comments="Çok bağlantılı indirme yöneticisi · Firefox eklentisi · yt-dlp ile video",
                                license_type=Gtk.License.MIT_X11)
        about.present(self.window)

    def _launch(self, path, folder=False):
        f = Gio.File.new_for_path(path)
        launcher = Gtk.FileLauncher.new(f)

        def done(l, res):
            try:
                (l.open_containing_folder_finish if folder else l.launch_finish)(res)
            except GLib.Error as e:
                if self.window:
                    self.window.toast(f"Açılamadı: {e.message}")

        if folder:
            launcher.open_containing_folder(self.window, None, done)
        else:
            launcher.launch(self.window, None, done)

    def open_file(self, did):
        self.withdraw(did)
        rec = self.manager.get(did)
        if not rec:
            return
        path = rec.get("path")
        vt = rec.get("vt") or {}
        dest = (rec.get("post") or {}).get("dest")
        if rec["status"] == "done" and (not path or not os.path.exists(path)) and dest and os.path.isdir(dest):
            self._launch(dest)
            return
        if rec["status"] == "done" and path and os.path.exists(path):
            if vt.get("verdict") == "malicious" or vt.get("mismatch"):
                reason = ("VirusTotal'da {}/{} motor bu dosyayı zararlı buldu.".format(
                    (vt.get("stats") or {}).get("malicious", 0), (vt.get("stats") or {}).get("total", 0))
                    if vt.get("verdict") == "malicious" else
                    "Dosyanın hash değeri sunucunun bildirdiğiyle uyuşmuyor; bozuk ya da değiştirilmiş olabilir.")
                dlg = Adw.AlertDialog(heading="Dosya yine de açılsın mı?", body=reason)
                dlg.add_response("cancel", "Vazgeç")
                dlg.add_response("open", "Yine de aç")
                dlg.set_response_appearance("open", Adw.ResponseAppearance.DESTRUCTIVE)
                dlg.connect("response", lambda d, r: self._launch(path) if r == "open" else None)
                dlg.present(self.window)
                return
            self._launch(path)
        elif self.window:
            self.window.toast("Dosya henüz hazır değil")

    def open_folder(self, did):
        self.withdraw(did)
        rec = self.manager.get(did)
        if not rec:
            return
        path = rec.get("path")
        if path and os.path.exists(path):
            self._launch(path, folder=True)
        elif path and os.path.exists(path + ".part"):
            self._launch(path + ".part", folder=True)
        else:
            d = rec.get("directory") or self.settings.get("download_dir")
            Path(d).mkdir(parents=True, exist_ok=True)
            self._launch(d)

    def set_autostart(self, enabled):
        autostart = CONFIG_DIR.parent / "autostart"
        target = autostart / f"{APP_ID}.desktop"
        if enabled:
            autostart.mkdir(parents=True, exist_ok=True)
            exe = os.path.expanduser("~/.local/bin/ddm")
            if not os.path.exists(exe):
                exe = f"{sys.executable} -m ddm"
            target.write_text(
                "[Desktop Entry]\nType=Application\n"
                f"Name={APP_NAME}\nExec={exe} --background\nIcon={APP_ID}\n"
                "X-GNOME-Autostart-enabled=true\nNoDisplay=true\n", encoding="utf-8")
        else:
            try:
                target.unlink()
            except OSError:
                pass
