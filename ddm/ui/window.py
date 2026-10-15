from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from ..bridge import valid_url
from ..util import CATEGORIES, filename_from_url, human_speed, looks_like_video_page
from .items import DownloadItem

FILTERS = {
    "all": lambda it: True,
    "active": lambda it: it.status in ("running", "queued", "paused", "error"),
    "done": lambda it: it.status == "done",
}


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.app = app
        self.set_default_size(1280, 700)
        self.set_title("DoDownloadManager")
        self.items = {}
        self.category = "all"
        self.search = ""
        self.last_clip = None

        self.store = Gio.ListStore(item_type=DownloadItem)
        self.filter = Gtk.CustomFilter.new(self._filter_func)
        self.filter_model = Gtk.FilterListModel(model=self.store, filter=self.filter)

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        split = Adw.NavigationSplitView()
        split.set_min_sidebar_width(200)
        split.set_max_sidebar_width(240)
        self.toasts.set_child(split)

        side_tb = Adw.ToolbarView()
        side_header = Adw.HeaderBar()
        side_header.set_title_widget(Adw.WindowTitle(title="DDM"))
        side_tb.add_top_bar(side_header)
        self.sidebar = Gtk.ListBox()
        self.sidebar.add_css_class("navigation-sidebar")
        self.sidebar.connect("row-selected", self._on_category)
        self.counts = {}
        for key, label, icon in CATEGORIES:
            row = Gtk.ListBoxRow()
            row.key = key
            box = Gtk.Box(spacing=12, margin_top=2, margin_bottom=2)
            box.append(Gtk.Image(icon_name=icon))
            box.append(Gtk.Label(label=label, xalign=0, hexpand=True))
            cnt = Gtk.Label()
            cnt.add_css_class("dim-label")
            cnt.add_css_class("numeric")
            box.append(cnt)
            self.counts[key] = cnt
            row.set_child(box)
            self.sidebar.append(row)
            if key == "video":
                row.set_header(Gtk.Separator(margin_top=6, margin_bottom=6, margin_start=6, margin_end=6))
        side_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        side_scroll.set_child(self.sidebar)
        side_tb.set_content(side_scroll)
        self.speed_label = Gtk.Label(xalign=0, margin_start=14, margin_end=14, margin_top=8, margin_bottom=10)
        self.speed_label.add_css_class("dim-label")
        self.speed_label.add_css_class("numeric")
        self.speed_label.add_css_class("caption")
        side_tb.add_bottom_bar(self.speed_label)
        split.set_sidebar(Adw.NavigationPage(title="Kategoriler", child=side_tb))

        content_tb = Adw.ToolbarView()
        header = Adw.HeaderBar()
        add_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Yeni indirme (Ctrl+N)")
        add_btn.set_action_name("app.add")
        header.pack_start(add_btn)
        for icon, action, tip in [("media-playback-start-symbolic", "win.resume", "Devam et"),
                                  ("media-playback-pause-symbolic", "win.pause", "Duraklat"),
                                  ("user-trash-symbolic", "win.remove", "Listeden kaldır (Del)")]:
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.set_action_name(action)
            header.pack_start(b)
        self.search_entry = Gtk.SearchEntry(placeholder_text="Ara…", width_chars=28)
        self.search_entry.connect("search-changed", self._on_search)
        header.set_title_widget(self.search_entry)
        menu = Gio.Menu()
        sec = Gio.Menu()
        sec.append("Tümünü sürdür", "app.resume-all")
        sec.append("Tümünü duraklat", "app.pause-all")
        sec.append("Tamamlananları temizle", "app.clear-finished")
        after = Gio.Menu()
        for label, target in (("Hiçbir şey yapma", "none"), ("Uyku moduna geç", "suspend"),
                              ("Bilgisayarı kapat", "poweroff")):
            it = Gio.MenuItem.new(label, None)
            it.set_action_and_target_value("app.after-all", GLib.Variant.new_string(target))
            after.append_item(it)
        sec.append_submenu("Tümü bitince", after)
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append("Ayarlar", "app.preferences")
        sec.append("DoDownloadManager hakkında", "app.about")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append("Çık", "app.quit")
        menu.append_section(None, sec)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True))
        self.after_icon = Gtk.Image(visible=False)
        self.after_icon.add_css_class("warning")
        header.pack_end(self.after_icon)
        content_tb.add_top_bar(header)

        self.columnview = self._build_columnview()
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.columnview)
        self.empty = Adw.StatusPage(icon_name="folder-download-symbolic", title="İndirme yok",
                                    description="Bir linki <b>Ctrl+V</b> ile yapıştırın, pencereye sürükleyin\n"
                                                "ya da Firefox eklentisini kullanın.")
        self.stack = Gtk.Stack()
        self.stack.add_named(scroll, "list")
        self.stack.add_named(self.empty, "empty")
        content_tb.set_content(self.stack)
        split.set_content(Adw.NavigationPage(title="İndirmeler", child=content_tb))
        self.filter_model.connect("items-changed", lambda *_: self._update_empty())

        self._install_actions()
        self._install_shortcuts()
        self._install_dnd()
        self.connect("notify::is-active", self._on_active)
        self.sidebar.select_row(self.sidebar.get_row_at_index(0))
        for rec in app.manager.records():
            self.add_record(rec)
        self._update_counts()
        self._update_empty()

    def _build_columnview(self):
        self.selection = Gtk.MultiSelection()
        cv = Gtk.ColumnView(model=self.selection, show_row_separators=False, reorderable=True)
        cv.add_css_class("data-table")
        cv.connect("activate", self._on_activate)

        def make(title, setup, bind, sorter=None, expand=False, fixed=None):
            f = Gtk.SignalListItemFactory()
            f.connect("setup", lambda _f, li: setup(li))
            f.connect("bind", lambda _f, li: self._bind_common(li, bind))
            f.connect("unbind", lambda _f, li: self._unbind_common(li))
            col = Gtk.ColumnViewColumn(title=title, factory=f, expand=expand, resizable=True)
            if fixed:
                col.set_fixed_width(fixed)
            if sorter:
                col.set_sorter(sorter)
            cv.append_column(col)
            return col

        def label(xalign=0.0, dim=False, numeric=False, ellip=Pango.EllipsizeMode.END):
            def setup(li):
                l = Gtk.Label(xalign=xalign, ellipsize=ellip)
                if dim:
                    l.add_css_class("dim-label")
                if numeric:
                    l.add_css_class("numeric")
                li.set_child(l)
            return setup

        def text_bind(prop):
            return lambda li, item: [item.bind_property(prop, li.get_child(), "label", GObject.BindingFlags.SYNC_CREATE)]

        def name_setup(li):
            box = Gtk.Box(spacing=10)
            box.append(Gtk.Image())
            l = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, hexpand=True)
            box.append(l)
            li.set_child(box)

        def name_bind(li, item):
            box = li.get_child()
            img = box.get_first_child()
            lbl = img.get_next_sibling()
            return [item.bind_property("icon_name", img, "icon-name", GObject.BindingFlags.SYNC_CREATE),
                    item.bind_property("name", lbl, "label", GObject.BindingFlags.SYNC_CREATE),
                    item.bind_property("name", box, "tooltip-text", GObject.BindingFlags.SYNC_CREATE)]

        def progress_setup(li):
            p = Gtk.ProgressBar(show_text=True, valign=Gtk.Align.CENTER)
            li.set_child(p)

        def progress_bind(li, item):
            p = li.get_child()
            return [item.bind_property("progress", p, "fraction", GObject.BindingFlags.SYNC_CREATE),
                    item.bind_property("progress_text", p, "text", GObject.BindingFlags.SYNC_CREATE)]

        def status_bind(li, item):
            lbl = li.get_child()
            return [item.bind_property("status_text", lbl, "label", GObject.BindingFlags.SYNC_CREATE),
                    item.bind_property("error", lbl, "tooltip-text", GObject.BindingFlags.SYNC_CREATE)]

        def prop_sorter(prop, numeric=False):
            expr = Gtk.PropertyExpression.new(DownloadItem, None, prop)
            return Gtk.NumericSorter.new(expr) if numeric else Gtk.StringSorter.new(expr)

        make("Ad", name_setup, name_bind, prop_sorter("name"), expand=True)
        make("Boyut", label(1.0, numeric=True), text_bind("size_text"), prop_sorter("total", True), fixed=140)
        make("İlerleme", progress_setup, progress_bind, prop_sorter("progress", True), fixed=120)
        make("Hız", label(1.0, numeric=True), text_bind("speed_text"), prop_sorter("speed", True), fixed=95)
        make("Kalan", label(1.0, numeric=True, dim=True), text_bind("eta_text"), None, fixed=80)
        make("Durum", label(0.0, ellip=Pango.EllipsizeMode.END), status_bind, prop_sorter("status_text"), fixed=120)

        def vt_setup(li):
            lbl = Gtk.Label(xalign=0.0)
            lbl.add_css_class("numeric")
            lbl.add_css_class("caption-heading")
            li.set_child(lbl)

        def vt_style(lbl, item):
            for c in ("success", "warning", "error", "dim-label", "accent"):
                lbl.remove_css_class(c)
            cls = {"ok": "success", "warn": "warning", "bad": "error", "unknown": "dim-label", "busy": "accent"}.get(item.vt_level)
            if cls:
                lbl.add_css_class(cls)

        def vt_bind(li, item):
            lbl = li.get_child()
            vt_style(lbl, item)
            hid = item.connect("notify::vt-level", lambda it, _p: vt_style(lbl, it))
            return [item.bind_property("vt_text", lbl, "label", GObject.BindingFlags.SYNC_CREATE),
                    item.bind_property("vt_tip", lbl, "tooltip-text", GObject.BindingFlags.SYNC_CREATE),
                    (item, hid)]

        make("VirusTotal", vt_setup, vt_bind, prop_sorter("vt_level"), fixed=105)
        created = make("Eklenme", label(1.0, dim=True, numeric=True), text_bind("created_text"),
                       prop_sorter("created", True), fixed=100)
        self.sort_model = Gtk.SortListModel(model=self.filter_model, sorter=cv.get_sorter())
        self.selection.set_model(self.sort_model)
        cv.sort_by_column(created, Gtk.SortType.DESCENDING)

        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._on_right_click)
        cv.add_controller(click)
        self.ctx_menu = Gio.Menu()
        s1 = Gio.Menu()
        s1.append("Aç", "win.open")
        s1.append("Klasörü aç", "win.open-folder")
        s1.append("Ayrıntılar", "win.details")
        self.ctx_menu.append_section(None, s1)
        s2 = Gio.Menu()
        s2.append("Devam et", "win.resume")
        s2.append("Duraklat", "win.pause")
        s2.append("Baştan başlat", "win.restart")
        s2.append("Linki yenile…", "win.refresh-link")
        s2.append("Zamanla", "win.schedule")
        s2.append("Zamanlamayı kaldır", "win.unschedule")
        self.ctx_menu.append_section(None, s2)
        s3 = Gio.Menu()
        s3.append("Linki kopyala", "win.copy-url")
        s3.append("SHA-256 kopyala", "win.copy-hash")
        s3.append("Sırada yukarı", "win.move-up")
        s3.append("Sırada aşağı", "win.move-down")
        self.ctx_menu.append_section(None, s3)
        svt = Gio.Menu()
        svt.append("VirusTotal'da aç", "win.vt-open")
        svt.append("VirusTotal'a yükle ve tara", "win.vt-upload")
        svt.append("Yeniden kontrol et", "win.vt-recheck")
        self.ctx_menu.append_section(None, svt)
        s4 = Gio.Menu()
        s4.append("Listeden kaldır", "win.remove")
        s4.append("Dosyayla birlikte sil", "win.delete")
        self.ctx_menu.append_section(None, s4)
        self.popover = Gtk.PopoverMenu.new_from_model(self.ctx_menu)
        self.popover.set_parent(cv)
        self.popover.set_has_arrow(False)
        self.popover.set_halign(Gtk.Align.START)
        self.selection.connect("selection-changed", lambda *_: self._update_action_state())
        return cv

    def _bind_common(self, li, bind):
        item = li.get_item()
        li.get_child()._dl_id = item.did
        li._bindings = bind(li, item)

    def _unbind_common(self, li):
        for b in getattr(li, "_bindings", None) or []:
            if isinstance(b, tuple):
                b[0].disconnect(b[1])
            else:
                b.unbind()
        li._bindings = None

    def _filter_func(self, item):
        if self.category in FILTERS:
            ok = FILTERS[self.category](item)
        else:
            ok = item.category == self.category
        if ok and self.search:
            q = self.search
            ok = q in item.name.casefold() or q in item.url.casefold()
        return ok

    def _on_category(self, box, row):
        if row is None or row.key is None:
            return
        self.category = row.key
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def _on_search(self, entry):
        self.search = entry.get_text().strip().casefold()
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def _update_empty(self):
        has = self.filter_model.get_n_items() > 0
        self.stack.set_visible_child_name("list" if has else "empty")
        if not has:
            if self.store.get_n_items() == 0:
                self.empty.set_title("İndirme yok")
                self.empty.set_description("Bir linki <b>Ctrl+V</b> ile yapıştırın, pencereye sürükleyin\n"
                                           "ya da Firefox eklentisini kullanın.")
            else:
                self.empty.set_title("Burada bir şey yok")
                self.empty.set_description("Bu kategoride ya da aramada eşleşen indirme bulunmuyor.")

    def _update_counts(self):
        counts = {k: 0 for k, _, _ in CATEGORIES}
        for i in range(self.store.get_n_items()):
            it = self.store.get_item(i)
            counts["all"] += 1
            if FILTERS["active"](it):
                counts["active"] += 1
            if it.status == "done":
                counts["done"] += 1
            if it.category in counts:
                counts[it.category] += 1
        for k, lbl in self.counts.items():
            lbl.set_label(str(counts[k]) if counts[k] else "")

    def add_record(self, rec):
        if rec["id"] in self.items:
            return
        item = DownloadItem(rec)
        self.items[rec["id"]] = item
        self.store.append(item)

    def on_manager_event(self, kind, rec):
        did = rec["id"]
        if kind == "added":
            self.add_record(rec)
        elif kind == "removed":
            item = self.items.pop(did, None)
            if item:
                ok, pos = self.store.find(item)
                if ok:
                    self.store.remove(pos)
        else:
            item = self.items.get(did)
            if item:
                old_status, old_cat = item.status, item.category
                item.update(rec, None)
                if old_status != item.status or old_cat != item.category:
                    self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_counts()
        self._update_action_state()

    def tick(self, progress):
        total_speed = 0.0
        for did, snap in progress.items():
            item = self.items.get(did)
            if item:
                rec = self.app.manager.get(did)
                if rec:
                    item.update(rec, snap)
                total_speed += snap.get("speed") or 0
        active = len(progress)
        sched = sum(1 for it in self.items.values() if it.status_text == "Zamanlandı")
        queued = sum(1 for it in self.items.values() if it.status == "queued") - sched
        m = self.app.manager
        parts = []
        if "metered" in m.holds:
            parts.append("Ölçülü bağlantı · bekliyor")
        if active:
            parts.append(f"{active} etkin")
        if queued > 0:
            parts.append(f"{queued} sırada")
        if sched:
            parts.append(f"{sched} zamanlandı ({self.app.settings.get('sched_start')})")
        if active:
            parts.append(human_speed(total_speed) if total_speed else "0 B/s")
        if m.metered_limit and "metered" not in m.holds:
            parts.append(f"ölçülü: ≤{m.metered_limit} KB/s")
        self.speed_label.set_label(" · ".join(parts) if parts else "Boşta")

    def set_after_indicator(self, mode):
        icons = {"suspend": ("weather-clear-night-symbolic", "Tüm indirmeler bitince uyku moduna geçilecek"),
                 "poweroff": ("system-shutdown-symbolic", "Tüm indirmeler bitince bilgisayar kapatılacak")}
        if mode in icons:
            self.after_icon.set_from_icon_name(icons[mode][0])
            self.after_icon.set_tooltip_text(icons[mode][1])
            self.after_icon.set_visible(True)
        else:
            self.after_icon.set_visible(False)

    def selected_ids(self):
        out = []
        bits = self.selection.get_selection()
        n = bits.get_size()
        for i in range(n):
            pos = bits.get_nth(i)
            item = self.sort_model.get_item(pos)
            if item:
                out.append(item.did)
        return out

    def _select_id(self, did):
        for i in range(self.sort_model.get_n_items()):
            if self.sort_model.get_item(i).did == did:
                if not self.selection.is_selected(i):
                    self.selection.select_item(i, True)
                return

    def _on_right_click(self, gesture, n, x, y):
        widget = self.columnview.pick(x, y, Gtk.PickFlags.DEFAULT)
        did = None
        while widget is not None:
            did = getattr(widget, "_dl_id", None)
            if did:
                break
            widget = widget.get_parent()
        if not did:
            return
        self._select_id(did)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self.popover.set_pointing_to(rect)
        self.popover.popup()

    def _on_activate(self, cv, pos):
        item = self.sort_model.get_item(pos)
        if not item:
            return
        if item.status == "done":
            self.app.open_file(item.did)
        else:
            self.app.show_details(item.did)

    def _install_actions(self):
        m = self.app.manager

        def each(fn):
            return lambda *_: [fn(i) for i in self.selected_ids()]

        def first(fn):
            def run(*_):
                ids = self.selected_ids()
                if ids:
                    fn(ids[0])
            return run

        defs = {
            "pause": each(m.pause),
            "resume": each(m.resume),
            "restart": each(m.restart),
            "open": first(self.app.open_file),
            "open-folder": first(self.app.open_folder),
            "details": first(self.app.show_details),
            "copy-url": self._copy_url,
            "copy-hash": self._copy_hash,
            "vt-open": first(self.app.vt_open),
            "vt-upload": first(self.app.vt_upload),
            "vt-recheck": each(m.vt_recheck),
            "refresh-link": first(self.app.refresh_link_dialog),
            "schedule": each(lambda i: m.set_scheduled(i, True)),
            "unschedule": each(lambda i: m.set_scheduled(i, False)),
            "move-up": each(lambda i: m.move(i, -1)),
            "move-down": each(lambda i: m.move(i, 1)),
            "remove": each(lambda i: m.remove(i)),
            "delete": self._confirm_delete,
            "paste": self._paste,
            "focus-search": lambda *_: self.search_entry.grab_focus(),
            "select-all": lambda *_: self.selection.select_all(),
        }
        self.actions = {}
        for name, cb in defs.items():
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
            self.actions[name] = a
        self._update_action_state()

    def _update_action_state(self):
        if not hasattr(self, "actions"):
            return
        ids = self.selected_ids()
        statuses = {self.items[i].status for i in ids if i in self.items}
        has = bool(ids)
        self.actions["pause"].set_enabled(bool(statuses & {"running", "queued"}))
        self.actions["resume"].set_enabled(bool(statuses & {"paused", "error"}))
        self.actions["restart"].set_enabled(bool(statuses - {"running"}) and has)
        self.actions["open"].set_enabled(statuses == {"done"} and len(ids) == 1)
        for n in ("open-folder", "details", "copy-url", "remove", "delete", "move-up", "move-down"):
            self.actions[n].set_enabled(has)
        sel = [self.items[i] for i in ids if i in self.items]
        one = sel[0] if len(sel) == 1 else None
        self.actions["copy-hash"].set_enabled(bool(one and one.sha256))
        self.actions["vt-open"].set_enabled(bool(one and (one.sha256 or one.vt_level)))
        self.actions["vt-upload"].set_enabled(bool(one and one.status == "done" and one.vt_level == "unknown"))
        self.actions["vt-recheck"].set_enabled(any(it.status == "done" for it in sel))
        recs = [self.app.manager.get(i) for i in ids]
        sched_flags = [bool(((r or {}).get("options") or {}).get("scheduled")) for r in recs if r]
        self.actions["schedule"].set_enabled(any(not f for f, it in zip(sched_flags, sel) if it.status != "done"))
        self.actions["unschedule"].set_enabled(any(sched_flags))
        self.actions["refresh-link"].set_enabled(bool(one and one.status in ("paused", "error") and one.kind == "file"))

    def _install_shortcuts(self):
        ctrl = Gtk.ShortcutController()
        ctrl.set_scope(Gtk.ShortcutScope.LOCAL)
        for accel, action in [("<Control>v", "win.paste"), ("Delete", "win.remove"),
                              ("<Shift>Delete", "win.delete"), ("<Control>f", "win.focus-search"),
                              ("space", "win.details"), ("<Control>a", "win.select-all")]:
            ctrl.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(accel),
                                               Gtk.NamedAction.new(action)))
        self.columnview.add_controller(ctrl)
        wctrl = Gtk.ShortcutController()
        wctrl.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
        wctrl.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("<Control>v"),
                                            Gtk.NamedAction.new("win.paste")))
        self.add_controller(wctrl)

    def _install_dnd(self):
        target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.COPY | Gdk.DragAction.LINK)
        target.connect("drop", self._on_drop)
        self.add_controller(target)

    def _on_drop(self, target, value, x, y):
        urls = [l.strip() for l in str(value or "").splitlines() if l.strip() and not l.startswith("#")]
        urls = [u for u in urls if valid_url(u)]
        if not urls:
            return False
        if len(urls) == 1:
            self.app.show_add({"url": urls[0]})
        else:
            for u in urls:
                self.app.manager.add(u)
            self.toast(f"{len(urls)} link kuyruğa eklendi")
        return True

    def _copy_url(self, *_):
        ids = self.selected_ids()
        urls = [self.items[i].url for i in ids if i in self.items]
        if urls:
            self.get_clipboard().set("\n".join(urls))
            self.toast("Link kopyalandı" if len(urls) == 1 else f"{len(urls)} link kopyalandı")

    def _copy_hash(self, *_):
        ids = self.selected_ids()
        if ids and ids[0] in self.items and self.items[ids[0]].sha256:
            self.get_clipboard().set(self.items[ids[0]].sha256)
            self.toast("SHA-256 kopyalandı")

    def _paste(self, *_):
        def done(c, res):
            try:
                text = (c.read_text_finish(res) or "").strip()
            except GLib.Error:
                text = ""
            urls = [u.strip() for u in text.splitlines() if valid_url(u.strip())]
            if len(urls) > 1:
                for u in urls:
                    self.app.manager.add(u)
                self.toast(f"{len(urls)} link kuyruğa eklendi")
            else:
                self.app.show_add({"url": urls[0]} if urls else {})

        self.get_clipboard().read_text_async(None, done)

    def _confirm_delete(self, *_):
        ids = self.selected_ids()
        if not ids:
            return
        dlg = Adw.AlertDialog(heading="Dosyalar silinsin mi?",
                              body=f"{len(ids)} indirme listeden kaldırılacak ve diskteki dosyaları silinecek.")
        dlg.add_response("cancel", "Vazgeç")
        dlg.add_response("delete", "Sil")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")

        def resp(d, r):
            if r == "delete":
                for i in ids:
                    self.app.manager.remove(i, delete_file=True)

        dlg.connect("response", resp)
        dlg.present(self)

    def _on_active(self, *_):
        if not self.is_active() or not self.app.settings.get("clipboard_watch"):
            return

        def done(c, res):
            try:
                text = (c.read_text_finish(res) or "").strip()
            except GLib.Error:
                return
            if not valid_url(text) or text == self.last_clip:
                return
            self.last_clip = text
            if any(it.url == text for it in self.items.values()):
                return
            name = filename_from_url(text)
            exts = set(self.app.settings.get("capture_exts") or [])
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext not in exts and not looks_like_video_page(text):
                return
            t = Adw.Toast(title=f"Panoda link var: {name or text[:60]}", button_label="İndir", timeout=8)
            t.connect("button-clicked", lambda *_: self.app.show_add({"url": text}))
            self.toasts.add_toast(t)

        self.get_clipboard().read_text_async(None, done)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))
