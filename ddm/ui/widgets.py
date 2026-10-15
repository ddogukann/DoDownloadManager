import math

from gi.repository import Gdk, Gtk

from ..util import human_speed


def color(widget, name, fallback):
    ok, c = widget.get_style_context().lookup_color(name)
    if ok:
        return c
    c = Gdk.RGBA()
    c.parse(fallback)
    return c


def rounded_rect(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class SegmentBar(Gtk.DrawingArea):
    def __init__(self, height=12):
        super().__init__()
        self.set_content_height(height)
        self.set_hexpand(True)
        self.total = 0
        self.segments = []
        self.fraction = 0.0
        self.done = False
        self.set_draw_func(self._draw)

    def update(self, total, segments, fraction, done=False):
        self.total = total or 0
        self.segments = segments or []
        self.fraction = max(0.0, min(1.0, fraction))
        self.done = done
        self.queue_draw()

    def _draw(self, area, cr, width, height):
        track = color(self, "card_shade_color", "#00000014")
        fg = color(self, "window_fg_color", "#000000")
        accent = color(self, "accent_bg_color", "#3584e4")
        ok = color(self, "success_bg_color", "#2ec27e")
        head = color(self, "accent_color", "#1c71d8")
        r = height / 2
        rounded_rect(cr, 0, 0, width, height, r)
        cr.save()
        cr.clip_preserve()
        Gdk.cairo_set_source_rgba(cr, track)
        cr.fill()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.06)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        fill = ok if self.done else accent
        if self.total and self.segments and not self.done:
            scale = width / self.total
            for s in self.segments:
                end = s["end"] if s.get("end") is not None else self.total - 1
                done_to = min(s["pos"], end + 1)
                if done_to > s["start"]:
                    Gdk.cairo_set_source_rgba(cr, fill)
                    cr.rectangle(s["start"] * scale, 0, max(1.0, (done_to - s["start"]) * scale), height)
                    cr.fill()
                if s.get("active") and done_to <= end:
                    Gdk.cairo_set_source_rgba(cr, head)
                    cr.rectangle(done_to * scale - 1.5, 0, 3, height)
                    cr.fill()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.18)
            for s in self.segments:
                if s["start"] > 0:
                    cr.rectangle(s["start"] * scale - 0.5, 0, 1, height)
                    cr.fill()
        else:
            Gdk.cairo_set_source_rgba(cr, fill)
            cr.rectangle(0, 0, width * (1.0 if self.done else self.fraction), height)
            cr.fill()
        cr.restore()


class SpeedGraph(Gtk.DrawingArea):
    def __init__(self, height=70):
        super().__init__()
        self.set_content_height(height)
        self.set_hexpand(True)
        self.samples = []
        self.set_draw_func(self._draw)

    def set_samples(self, samples):
        self.samples = list(samples or [])
        self.queue_draw()

    def _draw(self, area, cr, width, height):
        fg = color(self, "window_fg_color", "#000000")
        accent = color(self, "accent_bg_color", "#3584e4")
        pts = self.samples
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.08)
        for i in (1, 2, 3):
            y = round(height * i / 4) + 0.5
            cr.move_to(0, y)
            cr.line_to(width, y)
        cr.set_line_width(1)
        cr.stroke()
        if len(pts) < 2:
            return
        peak = max(pts) or 1.0
        top = 6
        usable = height - top - 2
        step = width / (len(pts) - 1)
        coords = [(i * step, height - 2 - (v / peak) * usable) for i, v in enumerate(pts)]
        cr.move_to(coords[0][0], height)
        for x, y in coords:
            cr.line_to(x, y)
        cr.line_to(coords[-1][0], height)
        cr.close_path()
        cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.18)
        cr.fill()
        cr.move_to(*coords[0])
        for x, y in coords[1:]:
            cr.line_to(x, y)
        cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.95)
        cr.set_line_width(2)
        cr.set_line_join(1)
        cr.stroke()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
        cr.select_font_face("Sans")
        cr.set_font_size(10)
        cr.move_to(4, 12)
        cr.show_text(human_speed(peak))


def chip(text="", style=None):
    lbl = Gtk.Label(label=text)
    lbl.add_css_class("ddm-chip")
    if style:
        lbl.add_css_class(style)
    return lbl


def set_chip(lbl, text, style):
    lbl.set_label(text)
    lbl.set_visible(bool(text))
    for c in ("success", "warning", "error", "accent", "dim"):
        lbl.remove_css_class(c)
    if style:
        lbl.add_css_class(style)


def stat_tile(title):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
    box.add_css_class("ddm-tile")
    value = Gtk.Label(xalign=0)
    value.add_css_class("ddm-tile-value")
    cap = Gtk.Label(label=title, xalign=0)
    cap.add_css_class("ddm-tile-caption")
    box.append(value)
    box.append(cap)
    return box, value
