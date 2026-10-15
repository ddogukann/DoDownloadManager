import os

from gi.repository import Adw, Gio, GLib, Gtk


def rule_summary(rule):
    bits = []
    if rule.get("exts"):
        bits.append(f"uzantı: {rule['exts']}")
    if rule.get("host"):
        bits.append(f"site: {rule['host']}")
    if rule.get("contains"):
        bits.append(f"adında: “{rule['contains']}”")
    target = []
    if rule.get("folder"):
        target.append(f"→ {rule['folder']}")
    acts = []
    if rule.get("extract"):
        acts.append("arşivi aç" + (" ve sil" if rule.get("delete_archive") else ""))
    if rule.get("open"):
        acts.append("bitince aç")
    text = " · ".join(bits) or "koşul yok"
    if target:
        text += "  " + " ".join(target)
    if acts:
        text += "  ·  " + ", ".join(acts)
    return text


class RuleEditor(Adw.Dialog):
    def __init__(self, settings, rule=None, index=None, on_saved=None):
        super().__init__()
        self.settings = settings
        self.index = index
        self.on_saved = on_saved
        rule = dict(rule or {})
        self.set_title("Kuralı düzenle" if index is not None else "Yeni kural")
        self.set_content_width(520)

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label="Vazgeç")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.save_btn = Gtk.Button(label="Kaydet")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", self._save)
        header.pack_end(self.save_btn)
        tv.add_top_bar(header)
        page = Adw.PreferencesPage()
        tv.set_content(page)
        self.set_child(tv)

        g = Adw.PreferencesGroup(title="Ne zaman", description="Doldurulan koşulların hepsi tutmalı")
        page.add(g)
        self.name = Adw.EntryRow(title="Kural adı")
        self.name.set_text(rule.get("name") or "")
        self.exts = Adw.EntryRow(title="Uzantılar (örn. deb, rpm, tar.gz)")
        self.exts.set_text(rule.get("exts") or "")
        self.host = Adw.EntryRow(title="Site (örn. github.com)")
        self.host.set_text(rule.get("host") or "")
        self.contains = Adw.EntryRow(title="Dosya adında geçen")
        self.contains.set_text(rule.get("contains") or "")
        for w in (self.name, self.exts, self.host, self.contains):
            g.add(w)

        g = Adw.PreferencesGroup(title="Ne yapılsın")
        page.add(g)
        self.folder = rule.get("folder") or ""
        self.folder_row = Adw.ActionRow(title="Klasör", subtitle=GLib.markup_escape_text(self.folder or "varsayılan"))
        pick = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
        pick.add_css_class("flat")
        pick.connect("clicked", self._pick)
        clear = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Varsayılana dön")
        clear.add_css_class("flat")
        clear.connect("clicked", lambda *_: self._set_folder(""))
        self.folder_row.add_suffix(clear)
        self.folder_row.add_suffix(pick)
        g.add(self.folder_row)
        self.extract = Adw.SwitchRow(title="Arşivse otomatik aç",
                                     subtitle="zip, tar.gz, tar.xz… · 7z/rar için 7zip paketi gerekir")
        self.extract.set_active(bool(rule.get("extract")))
        self.delete = Adw.SwitchRow(title="Açtıktan sonra arşivi sil")
        self.delete.set_active(bool(rule.get("delete_archive")))
        self.open = Adw.SwitchRow(title="Bitince aç", subtitle="Arşivse açılan klasörü, değilse dosyayı açar")
        self.open.set_active(bool(rule.get("open")))
        for w in (self.extract, self.delete, self.open):
            g.add(w)
        g.add(Adw.ActionRow(title="Güvenlik", subtitle="VirusTotal zararlı derse ya da checksum tutmazsa "
                                                      "arşiv açılmaz ve dosya otomatik açılmaz"))
        self.extract.connect("notify::active", lambda *_: self.delete.set_sensitive(self.extract.get_active()))
        self.delete.set_sensitive(self.extract.get_active())
        self.enabled = rule.get("enabled", True)

    def _set_folder(self, path):
        self.folder = path
        self.folder_row.set_subtitle(GLib.markup_escape_text(path or "varsayılan"))

    def _pick(self, *_):
        dlg = Gtk.FileDialog(title="Kural klasörü")
        start = os.path.expanduser(self.folder or self.settings.get("download_dir"))
        if os.path.isdir(start):
            dlg.set_initial_folder(Gio.File.new_for_path(start))

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                home = os.path.expanduser("~")
                p = f.get_path()
                self._set_folder("~" + p[len(home):] if p.startswith(home + os.sep) else p)

        dlg.select_folder(self.get_root(), None, done)

    def _save(self, *_):
        rule = {
            "name": self.name.get_text().strip() or "Kural",
            "exts": ", ".join(e.strip().lstrip(".") for e in self.exts.get_text().split(",") if e.strip()),
            "host": self.host.get_text().strip().lower().removeprefix("www."),
            "contains": self.contains.get_text().strip(),
            "folder": self.folder,
            "extract": self.extract.get_active(),
            "delete_archive": self.extract.get_active() and self.delete.get_active(),
            "open": self.open.get_active(),
            "enabled": self.enabled,
        }
        if not (rule["exts"] or rule["host"] or rule["contains"]):
            self.exts.add_css_class("error")
            return
        rules = list(self.settings.get("rules") or [])
        if self.index is None:
            rules.append(rule)
        else:
            rules[self.index] = rule
        self.settings.set("rules", rules)
        if self.on_saved:
            self.on_saved()
        self.close()


class RulesPage(Adw.PreferencesPage):
    def __init__(self, dialog, settings):
        super().__init__(title="Kurallar", icon_name="view-list-bullet-symbolic")
        self.dialog = dialog
        self.settings = settings
        self.group = Adw.PreferencesGroup(
            title="Kurallar",
            description="Yukarıdan aşağı ilk eşleşen kural uygulanır. Ekleme penceresinde elle klasör seçersen kural klasörü kullanılmaz.")
        add = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Kural ekle")
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: self._edit(None))
        self.group.set_header_suffix(add)
        self.add(self.group)
        self.rows = []
        self.empty = Adw.ActionRow(title="Henüz kural yok",
                                   subtitle="Örnek: uzantı “deb, rpm” → ~/Paketler; uzantı “zip” → arşivi aç ve sil")
        self.refresh()

    def refresh(self):
        for r in self.rows:
            self.group.remove(r)
        self.rows = []
        rules = self.settings.get("rules") or []
        if not rules:
            self.group.add(self.empty)
            self.rows.append(self.empty)
            return
        for i, rule in enumerate(rules):
            row = Adw.ActionRow(title=GLib.markup_escape_text(rule.get("name") or "Kural"),
                                subtitle=GLib.markup_escape_text(rule_summary(rule)))
            row.set_subtitle_lines(2)
            sw = Gtk.Switch(active=rule.get("enabled", True), valign=Gtk.Align.CENTER)
            sw.connect("notify::active", lambda s, _p, idx=i: self._toggle(idx, s.get_active()))
            row.add_prefix(sw)
            for icon, tip, cb in (("go-up-symbolic", "Yukarı", lambda *_, idx=i: self._move(idx, -1)),
                                  ("document-edit-symbolic", "Düzenle", lambda *_, idx=i: self._edit(idx)),
                                  ("user-trash-symbolic", "Sil", lambda *_, idx=i: self._delete(idx))):
                b = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER, tooltip_text=tip)
                b.add_css_class("flat")
                b.connect("clicked", cb)
                if icon == "go-up-symbolic" and i == 0:
                    b.set_sensitive(False)
                row.add_suffix(b)
            self.group.add(row)
            self.rows.append(row)

    def _rules(self):
        return [dict(r) for r in (self.settings.get("rules") or [])]

    def _toggle(self, idx, on):
        rules = self._rules()
        rules[idx]["enabled"] = on
        self.settings.set("rules", rules)

    def _move(self, idx, delta):
        rules = self._rules()
        j = max(0, min(len(rules) - 1, idx + delta))
        rules.insert(j, rules.pop(idx))
        self.settings.set("rules", rules)
        self.refresh()

    def _delete(self, idx):
        rules = self._rules()
        rules.pop(idx)
        self.settings.set("rules", rules)
        self.refresh()

    def _edit(self, idx):
        rules = self._rules()
        editor = RuleEditor(self.settings, rules[idx] if idx is not None else None, idx, on_saved=self.refresh)
        editor.present(self.dialog)
