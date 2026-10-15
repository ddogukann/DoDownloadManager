import math
import os
import time
from pathlib import Path

import cairo
from gi.repository import Gio, GLib

from . import APP_ID, APP_NAME
from .util import human_eta, human_size, human_speed

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="XAyatanaLabel" type="s" access="read"/>
    <property name="XAyatanaLabelGuide" type="s" access="read"/>
    <property name="XAyatanaOrderingIndex" type="u" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
    <signal name="XAyatanaNewLabel"><arg name="label" type="s"/><arg name="guide" type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timestamp"/>
    </signal>
  </interface>
</node>
"""

ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER = "org.kde.StatusNotifierWatcher"

ID_SHOW, ID_ADD, ID_PAUSE, ID_RESUME, ID_PREFS, ID_QUIT, ID_EMPTY, ID_HEADER, ID_RECENT_HEAD = 1, 2, 3, 4, 5, 6, 7, 8, 9
SEP_A, SEP_B, SEP_C, SEP_D = 50, 51, 52, 53
DL_BASE = 1000
RECENT_BASE = 5000
MAX_ITEMS = 6
MAX_RECENT = 3
ICON_SIZES = (22, 32, 44)


def _shorten(name, n=38):
    return name if len(name) <= n else name[: n - 1] + "…"


def _menu_text(text):
    return text.replace("_", "__")


def _text_bar(frac, cells=10):
    filled = max(0, min(cells, int(round(frac * cells))))
    return "▰" * filled + "▱" * (cells - filled)


def render_icon(size, frac, paused=False):
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surf)
    lw = max(2.0, size * 0.12)
    r = size / 2 - lw / 2 - size * 0.02
    c = size / 2
    cr.set_line_width(lw)
    cr.set_source_rgba(1, 1, 1, 0.28)
    cr.arc(c, c, r, 0, 2 * math.pi)
    cr.stroke()
    if frac > 0:
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(1, 1, 1, 0.6 if paused else 1.0)
        cr.arc(c, c, r, -math.pi / 2, -math.pi / 2 + 2 * math.pi * min(1.0, frac))
        cr.stroke()
    cr.set_source_rgba(1, 1, 1, 1)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    inner = r - lw / 2
    if paused:
        cr.set_line_width(max(1.6, size * 0.1))
        for dx in (-0.22, 0.22):
            cr.move_to(c + dx * inner, c - 0.38 * inner)
            cr.line_to(c + dx * inner, c + 0.38 * inner)
        cr.stroke()
    else:
        cr.set_line_width(max(1.6, size * 0.09))
        cr.move_to(c, c - 0.5 * inner)
        cr.line_to(c, c + 0.42 * inner)
        cr.move_to(c - 0.36 * inner, c + 0.06 * inner)
        cr.line_to(c, c + 0.42 * inner)
        cr.line_to(c + 0.36 * inner, c + 0.06 * inner)
        cr.stroke()
    surf.flush()
    data = bytes(surf.get_data())
    stride = surf.get_stride()
    out = bytearray()
    for y in range(size):
        row = data[y * stride: y * stride + size * 4]
        for x in range(size):
            b, g, rr, a = row[x * 4: x * 4 + 4]
            if a and a < 255:
                rr, g, b = min(255, rr * 255 // a), min(255, g * 255 // a), min(255, b * 255 // a)
            out += bytes((a, rr, g, b))
    return bytes(out)


class TrayIcon:
    def __init__(self, app):
        self.app = app
        self.conn = None
        self.reg_ids = []
        self.name_id = 0
        self.watch_id = 0
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.revision = 1
        self.label = ""
        self.tooltip = APP_NAME
        self.items = {}
        self.structure = []
        self.dl_ids = {}
        self.next_dl = DL_BASE
        self.recent_ids = {}
        self.started_at = time.time()
        self.pixmaps = []
        self.icon_key = None
        self.icon_theme_path = str(Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "icons")
        self.sni_info = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        self.menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self.items, self.structure, self.label, self.tooltip, _ = self._rebuild({})

    def start(self):
        if self.conn is not None:
            return
        try:
            self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            self.conn = None
            return
        self.reg_ids.append(self.conn.register_object(ITEM_PATH, self.sni_info, self._sni_call, self._sni_prop, None))
        self.reg_ids.append(self.conn.register_object(MENU_PATH, self.menu_info, self._menu_call, self._menu_prop, None))
        self.name_id = Gio.bus_own_name_on_connection(self.conn, self.bus_name, Gio.BusNameOwnerFlags.NONE, None, None)
        self.watch_id = Gio.bus_watch_name_on_connection(self.conn, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                                         self._watcher_appeared, None)

    def stop(self):
        if self.conn is None:
            return
        if self.watch_id:
            Gio.bus_unwatch_name(self.watch_id)
            self.watch_id = 0
        if self.name_id:
            Gio.bus_unown_name(self.name_id)
            self.name_id = 0
        for rid in self.reg_ids:
            self.conn.unregister_object(rid)
        self.reg_ids = []
        self.conn = None

    def _watcher_appeared(self, conn, name, owner):
        conn.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                  GLib.Variant("(s)", (self.bus_name,)), None, Gio.DBusCallFlags.NONE, -1, None, None)

    def _emit(self, path, iface, name, params=None):
        if self.conn is None:
            return
        try:
            self.conn.emit_signal(None, path, iface, name, params)
        except GLib.Error:
            pass

    def _sni_prop(self, conn, sender, path, iface, prop):
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", APP_ID),
            "Title": GLib.Variant("s", APP_NAME),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", "" if self.pixmaps else f"{APP_ID}-symbolic"),
            "IconThemePath": GLib.Variant("s", self.icon_theme_path),
            "IconPixmap": GLib.Variant("a(iiay)", self.pixmaps),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], APP_NAME, self.tooltip)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
            "XAyatanaLabel": GLib.Variant("s", self.label),
            "XAyatanaLabelGuide": GLib.Variant("s", "%99 · 999,9 MB/s"),
            "XAyatanaOrderingIndex": GLib.Variant("u", 0),
        }
        return values.get(prop)

    def _sni_call(self, conn, sender, path, iface, method, params, invocation):
        if method == "Activate":
            GLib.idle_add(self._do, ID_SHOW)
        elif method == "SecondaryActivate":
            GLib.idle_add(self._do, ID_ADD)
        invocation.return_value(None)

    def _menu_prop(self, conn, sender, path, iface, prop):
        values = {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", [self.icon_theme_path]),
        }
        return values.get(prop)

    @staticmethod
    def _props_variant(props):
        out = {}
        for k, v in props.items():
            if isinstance(v, bool):
                out[k] = GLib.Variant("b", v)
            elif isinstance(v, int):
                out[k] = GLib.Variant("i", v)
            else:
                out[k] = GLib.Variant("s", v)
        return out

    def _node(self, iid, depth):
        props = self._props_variant(self.items.get(iid, {}))
        children = []
        if iid == 0 and depth != 0:
            for cid in self.structure:
                children.append(GLib.Variant("(ia{sv}av)", self._node(cid, depth - 1 if depth > 0 else -1)))
        return (iid, props, children)

    def _menu_call(self, conn, sender, path, iface, method, params, invocation):
        args = params.unpack()
        if method == "GetLayout":
            parent, depth, _ = args
            if parent != 0 and parent not in self.items:
                parent = 0
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._node(parent, depth))))
        elif method == "GetGroupProperties":
            ids, _ = args
            ids = ids or [0] + self.structure
            out = [(i, self._props_variant(self.items[i])) for i in ids if i in self.items]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (out,)))
        elif method == "GetProperty":
            iid, name = args
            props = self._props_variant(self.items.get(iid, {}))
            invocation.return_value(GLib.Variant("(v)", (props.get(name, GLib.Variant("s", "")),)))
        elif method == "Event":
            iid, event, _, _ = args
            if event == "clicked":
                GLib.idle_add(self._do, iid)
            invocation.return_value(None)
        elif method == "EventGroup":
            for iid, event, _, _ in args[0]:
                if event == "clicked":
                    GLib.idle_add(self._do, iid)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    def _do(self, iid):
        app = self.app
        if iid == ID_SHOW:
            app.present_window()
        elif iid == ID_ADD:
            app.show_add({})
        elif iid == ID_PAUSE:
            app.manager.pause_all()
        elif iid == ID_RESUME:
            app.manager.resume_all()
        elif iid == ID_PREFS:
            app.show_preferences()
        elif iid == ID_QUIT:
            app.quit_app()
        elif iid >= RECENT_BASE:
            did = next((d for d, i in self.recent_ids.items() if i == iid), None)
            if did:
                app.open_file(did)
        elif iid >= DL_BASE:
            did = next((d for d, i in self.dl_ids.items() if i == iid), None)
            if did:
                app.present_window()
                app.show_details(did)
        return False

    def _rebuild(self, progress):
        records = self.app.manager.records() if self.app.manager else []
        running = sorted((r for r in records if r["status"] == "running"), key=lambda r: r.get("created") or 0)
        queued = [r for r in records if r["status"] == "queued"]
        paused = [r for r in records if r["status"] in ("paused", "error")]
        recent = sorted((r for r in records if r["status"] == "done" and (r.get("finished") or 0) >= self.started_at),
                        key=lambda r: -(r.get("finished") or 0))[:MAX_RECENT]
        total_speed = 0.0
        sum_done = sum_total = 0
        for r in running:
            snap = progress.get(r["id"]) or {}
            total_speed += snap.get("speed") or 0
            t = snap.get("total") or r.get("total")
            if t:
                sum_total += t
                sum_done += snap.get("downloaded") or 0
        overall = sum_done / sum_total if sum_total else None

        items = {0: {"children-display": "submenu"}}
        structure = []
        if running:
            head = f"{len(running)} indiriliyor"
            if total_speed:
                head += f"  ·  {human_speed(total_speed)}"
            if sum_total and total_speed:
                head += f"  ·  ~{human_eta((sum_total - sum_done) / total_speed)}"
            if queued:
                head += f"  ·  {len(queued)} sırada"
        elif queued:
            head = f"{len(queued)} indirme sırada"
        elif paused:
            head = f"{len(paused)} indirme duraklatıldı"
        else:
            head = "Etkin indirme yok"
        items[ID_HEADER] = {"label": _menu_text(head), "enabled": False}
        structure.append(ID_HEADER)

        shown = (running + queued + paused)[:MAX_ITEMS]
        for r in shown:
            iid = self.dl_ids.get(r["id"])
            if iid is None:
                iid = self.next_dl
                self.next_dl += 1
                self.dl_ids[r["id"]] = iid
            snap = progress.get(r["id"]) or {}
            name = _shorten(r.get("filename") or r["url"].rsplit("/", 1)[-1] or r["url"])
            total = snap.get("total") or r.get("total")
            done = snap.get("downloaded") if snap else r.get("downloaded")
            done = done or 0
            frac = done / total if total else 0.0
            if r["status"] == "running":
                spd = snap.get("speed") or 0
                tail = f"%{int(frac * 100)}" if total else human_size(done)
                text = f"{_text_bar(frac)}  {tail}   {name}" + (f"  ·  {human_speed(spd)}" if spd else "")
                icon = "folder-download-symbolic"
            elif r["status"] == "queued":
                text = f"{_text_bar(frac)}  sırada   {name}"
                icon = "content-loading-symbolic"
            elif r["status"] == "paused":
                text = f"{_text_bar(frac)}  %{int(frac * 100)}   {name}  ·  duraklatıldı" if total else f"{name}  ·  duraklatıldı"
                icon = "media-playback-pause-symbolic"
            else:
                text = f"{name}  ·  hata"
                icon = "dialog-warning-symbolic"
            items[iid] = {"label": _menu_text(text), "icon-name": icon}
            structure.append(iid)
        extra = len(running) + len(queued) + len(paused) - len(shown)
        if extra > 0:
            items[ID_EMPTY] = {"label": f"+{extra} indirme daha", "enabled": False}
            structure.append(ID_EMPTY)

        live = {r["id"] for r in shown}
        self.dl_ids = {d: i for d, i in self.dl_ids.items() if d in live}
        if recent:
            items[SEP_C] = {"type": "separator"}
            items[ID_RECENT_HEAD] = {"label": "Son tamamlananlar", "enabled": False}
            structure += [SEP_C, ID_RECENT_HEAD]
            for n, r in enumerate(recent):
                iid = RECENT_BASE + n
                self.recent_ids = {d: i for d, i in self.recent_ids.items() if i != iid}
                self.recent_ids[r["id"]] = iid
                name = _shorten(r.get("filename") or r["url"].rsplit("/", 1)[-1])
                size = human_size(r.get("total")) if r.get("total") else ""
                vt = (r.get("vt") or {}).get("verdict")
                mark = {"malicious": "  ·  ✕ VirusTotal", "suspicious": "  ·  ⚠ VirusTotal"}.get(vt, "")
                items[iid] = {"label": _menu_text(f"{name}  ·  {size}{mark}"), "icon-name": "object-select-symbolic"}
                structure.append(iid)

        items[SEP_A] = {"type": "separator"}
        items[ID_SHOW] = {"label": "Pencereyi aç", "icon-name": "window-new-symbolic"}
        items[ID_ADD] = {"label": "Yeni indirme…", "icon-name": "list-add-symbolic"}
        items[SEP_D] = {"type": "separator"}
        items[ID_PAUSE] = {"label": "Tümünü duraklat", "enabled": bool(running or queued),
                           "icon-name": "media-playback-pause-symbolic"}
        items[ID_RESUME] = {"label": "Tümünü sürdür", "enabled": bool(paused),
                            "icon-name": "media-playback-start-symbolic"}
        items[SEP_B] = {"type": "separator"}
        items[ID_PREFS] = {"label": "Ayarlar", "icon-name": "emblem-system-symbolic"}
        items[ID_QUIT] = {"label": "Çık", "icon-name": "application-exit-symbolic"}
        structure += [SEP_A, ID_SHOW, ID_ADD, SEP_D, ID_PAUSE, ID_RESUME, SEP_B, ID_PREFS, ID_QUIT]

        if running:
            label = (f"%{int(overall * 100)}" if overall is not None else "")
            if total_speed:
                label = f"{label} · {human_speed(total_speed)}" if label else human_speed(total_speed)
            tip = head
            icon_key = ("run", int((overall or 0) * 50))
        elif queued or paused:
            label, tip = "", head
            fr = 0.0
            if paused:
                t = sum(r.get("total") or 0 for r in paused)
                fr = (sum(r.get("downloaded") or 0 for r in paused) / t) if t else 0.0
            icon_key = ("pause", int(fr * 50)) if paused and not queued else ("queue", 0)
        else:
            label, tip = "", "Boşta"
            icon_key = None
        return items, structure, label, tip, icon_key

    def update(self, progress, show_label=True):
        items, structure, label, tip, icon_key = self._rebuild(progress)
        if not show_label:
            label = ""
        if icon_key != self.icon_key:
            self.icon_key = icon_key
            if icon_key is None:
                self.pixmaps = []
            else:
                kind, step = icon_key
                frac = step / 50 if kind != "queue" else 0.0
                self.pixmaps = [(sz, sz, render_icon(sz, frac, paused=(kind == "pause"))) for sz in ICON_SIZES]
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewIcon")
        if structure != self.structure:
            self.items, self.structure = items, structure
            self.revision += 1
            self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated", GLib.Variant("(ui)", (self.revision, 0)))
        else:
            changed = [(i, self._props_variant(p)) for i, p in items.items() if self.items.get(i) != p]
            self.items = items
            if changed:
                self._emit(MENU_PATH, "com.canonical.dbusmenu", "ItemsPropertiesUpdated",
                           GLib.Variant("(a(ia{sv})a(ias))", (changed, [])))
        if label != self.label:
            self.label = label
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "XAyatanaNewLabel",
                       GLib.Variant("(ss)", (label, "%99 · 999,9 MB/s")))
        if tip != self.tooltip:
            self.tooltip = tip
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip")
