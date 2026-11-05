"""Components shared between pages."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Gdk, GLib, Gtk

from ...i18n import _
from ...services.logos import logo_loader
from ...services.text import normalize

SEARCH_DELAY_MS = 220


class GroupColumn:
    """The group list beside the content (TV channels, movies, series), as on
    the TV apps: while the focus is in the content it folds to a narrow
    "Groups" strip, so the list or the posters get the room; a click on the
    strip (or reaching it with the keyboard) opens it again on the selected
    group."""

    def __init__(self, listbox: Gtk.ListBox, width: int = 250):
        self.list = listbox
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                    vexpand=True, hexpand=True)
        scroll.set_child(listbox)
        self.panel = Gtk.Box()
        self.panel.add_css_class("ashy-panel")
        self.panel.set_size_request(width, -1)
        self.panel.set_hexpand(False)  # don't inherit the inner list's hexpand
        self.panel.set_overflow(Gtk.Overflow.HIDDEN)
        self.panel.append(scroll)
        strip = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                        valign=Gtk.Align.START, margin_top=14)
        strip.append(Gtk.Image(icon_name="view-list-symbolic", pixel_size=22))
        strip.append(Gtk.Label(label=_("Groups")))
        self.rail = Gtk.Button(child=strip, visible=False, tooltip_text=_("Groups"))
        self.rail.add_css_class("ashy-panel")
        self.rail.add_css_class("ashy-group-rail")
        self.rail.set_size_request(76, -1)
        self.rail.connect("clicked", lambda _b: self.unfold())
        # Reached with the keyboard (◀ from the list, Tab): open at once.
        keys = Gtk.EventControllerFocus()
        keys.connect("enter", lambda _c: GLib.idle_add(self._unfold_once))
        self.rail.add_controller(keys)

    def append_to(self, box: Gtk.Box) -> None:
        box.append(self.panel)
        box.append(self.rail)

    def watch(self, content: Gtk.Widget) -> None:
        """The focus entering `content` folds the column."""
        ctl = Gtk.EventControllerFocus()
        ctl.connect("enter", lambda _c: self.fold())
        content.add_controller(ctl)

    def fold(self) -> None:
        if self.panel.get_visible():
            self.panel.set_visible(False)
            self.rail.set_visible(True)

    def show(self) -> Gtk.Widget:
        """Open (without moving the focus) and return the column (guided tour)."""
        self.rail.set_visible(False)
        self.panel.set_visible(True)
        return self.panel

    def _unfold_once(self) -> bool:
        if self.rail.get_visible():
            self.unfold()
        return GLib.SOURCE_REMOVE

    def unfold(self) -> None:
        self.show()
        row = self.list.get_selected_row()
        if row is not None:
            row.grab_focus()
        else:
            self.list.child_focus(Gtk.DirectionType.TAB_FORWARD)


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


def update_favorite(store, channel) -> list[int]:
    """Copy `channel.favorite` to the matching items of a ChannelObject store
    (the caller repaints its bound rows). Returns the positions found. Far
    cheaper than reloading the whole list, and keeps scroll and focus."""
    found = []
    for i in range(store.get_n_items()):
        ch = store.get_item(i).channel
        if ch.playlist_id == channel.playlist_id and ch.url == channel.url:
            ch.favorite = channel.favorite
            found.append(i)
    return found


def is_menu_key(keyval: int, state) -> bool:
    """The keyboard's context menu keys: Menu and Shift+F10."""
    return keyval == Gdk.KEY_Menu or (
        keyval == Gdk.KEY_F10 and bool(state & Gdk.ModifierType.SHIFT_MASK))


def add_context_menu(widget: Gtk.Widget, open_menu: Callable[[], None],
                     keys: bool = True) -> None:
    """Right click, long press (touch) and — when `widget` takes the focus
    itself — the Menu key / Shift+F10 call open_menu()."""
    def pressed(gesture, *_a):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        open_menu()

    click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
    click.connect("pressed", pressed)
    widget.add_controller(click)
    press = Gtk.GestureLongPress(touch_only=True)
    press.connect("pressed", pressed)
    widget.add_controller(press)
    if keys:
        def key_pressed(_c, keyval, _code, state) -> bool:
            if not is_menu_key(keyval, state):
                return False
            open_menu()
            return True

        ctrl = Gtk.EventControllerKey()
        ctrl.connect("key-pressed", key_pressed)
        widget.add_controller(ctrl)


def popup_menu(anchor: Gtk.Widget, items: list[tuple[str, Callable[[], None]]]) -> Gtk.Popover:
    """A small popover of flat buttons pointing at `anchor`."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    popover = Gtk.Popover(child=box)
    popover.set_parent(anchor)
    fill_menu(box, popover, items)
    popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
    popover.popup()
    return popover


def fill_menu(box: Gtk.Box, popover: Gtk.Popover,
              items: list[tuple[str, Callable[[], None]]]) -> None:
    for label, callback in items:
        b = Gtk.Button(label=label)
        b.add_css_class("flat")
        b.get_child().set_xalign(0)
        b.connect("clicked", lambda _b, cb=callback: (popover.popdown(), cb()))
        box.append(b)
