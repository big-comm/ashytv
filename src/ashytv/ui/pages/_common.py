"""Components shared between pages."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import GLib, Gtk

from ...services.logos import logo_loader
from ...services.text import normalize

SEARCH_DELAY_MS = 220


def page_header(title: str, subtitle: str = "") -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.set_margin_top(18)
    box.set_margin_start(28)
    box.set_margin_end(28)
    box.set_margin_bottom(10)

    t = Gtk.Label(label=title, xalign=0)
    t.add_css_class("ashy-page-title")
    box.append(t)

    if subtitle:
        s = Gtk.Label(label=subtitle, xalign=0)
        s.add_css_class("ashy-page-subtitle")
        box.append(s)
    return box


class ChipRow(Gtk.ScrolledWindow):
    """Scrollable row of filter chips (single selection)."""

    def __init__(self, on_selected: Callable[[str], None]):
        super().__init__(vscrollbar_policy=Gtk.PolicyType.NEVER,
                         hscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        self.set_margin_start(28)
        self.set_margin_end(28)
        self.set_margin_bottom(12)
        self._box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.set_child(self._box)
        self._on_selected = on_selected
        self._buttons: dict[str, Gtk.Button] = {}
        self.active = ""

    def set_chips(self, chips: list[tuple[str, str]], active: str = "") -> None:
        """chips = [(key, label)]."""
        while (child := self._box.get_first_child()) is not None:
            self._box.remove(child)
        self._buttons.clear()
        for key, label in chips:
            btn = Gtk.Button(label=label)
            btn.add_css_class("ashy-chip")
            btn.connect("clicked", lambda _b, k=key: self.select(k))
            self._buttons[key] = btn
            self._box.append(btn)
        self._mark(active or (chips[0][0] if chips else ""))

    def select(self, key: str) -> None:
        self._mark(key)
        self._on_selected(key)

    def _mark(self, key: str) -> None:
        self.active = key
        for k, btn in self._buttons.items():
            if k == key:
                btn.add_css_class("selected")
            else:
                btn.remove_css_class("selected")


class SearchFilter:
    """Search filter with debounce and incremental change (stricter/looser)."""

    def __init__(self, on_change: Callable[[], None] | None = None):
        self.text = ""
        self.filter = Gtk.CustomFilter.new(self._match, None)
        self._pending = ""
        self._source = 0
        self._on_change = on_change

    def _match(self, item, _data) -> bool:
        return not self.text or self.text in item.key

    def set_text(self, text: str) -> None:
        self._pending = normalize(text.strip())
        if self._source:
            GLib.source_remove(self._source)
        self._source = GLib.timeout_add(SEARCH_DELAY_MS, self._apply)

    def _apply(self) -> bool:
        self._source = 0
        old, new = self.text, self._pending
        if old == new:
            return GLib.SOURCE_REMOVE
        self.text = new
        # "not" -> "news" only removes items; the reverse only adds.
        if old in new:
            change = Gtk.FilterChange.MORE_STRICT
        elif new in old:
            change = Gtk.FilterChange.LESS_STRICT
        else:
            change = Gtk.FilterChange.DIFFERENT
        self.filter.changed(change)
        if self._on_change:
            self._on_change()
        return GLib.SOURCE_REMOVE


def set_logo(image: Gtk.Image, url: str, size: int, fallback: str,
             still_valid: Callable[[], bool] = lambda: True) -> None:
    """Show the cached logo or the icon, and load in the background."""
    tex = logo_loader.cached_texture(url, size) if url else None
    if tex is not None:
        image.set_from_paintable(tex)
        return
    image.set_from_icon_name(fallback)
    if url:
        def apply(texture):
            if still_valid():
                image.set_from_paintable(texture)
        logo_loader.load(url, apply, size)


def set_picture(picture: Gtk.Picture, url: str, size: int,
                still_valid: Callable[[], bool] = lambda: True) -> None:
    """Like set_logo, for Gtk.Picture (posters/covers)."""
    tex = logo_loader.cached_texture(url, size) if url else None
    picture.set_paintable(tex)
    if tex is None and url:
        def apply(texture):
            if still_valid():
                picture.set_paintable(texture)
        logo_loader.load(url, apply, size)


def clear_box(box: Gtk.Widget) -> None:
    while (child := box.get_first_child()) is not None:
        box.remove(child)


def scroller(child: Gtk.Widget, **kwargs) -> Gtk.ScrolledWindow:
    sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True, **kwargs)
    sw.set_child(child)
    return sw
