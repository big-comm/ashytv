"""Hidden channels and groups: see them and show them again."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk

from ..i18n import _
from ..models import MediaType
from .pages._common import set_logo


class HiddenDialog(Adw.Dialog):
    __gtype_name__ = "AshyHiddenDialog"

    def __init__(self, storage, on_changed: Callable[[], None]):
        super().__init__(title=_("Hidden channels and groups"), content_width=560,
                         content_height=560)
        self.storage = storage
        self._on_changed = on_changed
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._show_all = Gtk.Button(label=_("Show all"))
        self._show_all.connect("clicked", lambda *_: self._apply(None))
        header.pack_start(self._show_all)
        view.add_top_bar(header)
        self._page = Adw.PreferencesPage()
        view.set_content(self._page)
        self.set_child(view)
        self._fill()

    def _fill(self) -> None:
        for group in list(self._groups()):
            self._page.remove(group)
        items = self.storage.hidden_entries()
        self._show_all.set_sensitive(bool(items))
        if not items:
            empty = Adw.PreferencesGroup()
            status = Adw.StatusPage(icon_name="view-conceal-symbolic",
                                    title=_("Nothing hidden"),
                                    description=_("Right-click (or long-press) a group or "
                                                  "channel to hide it."))
            status.add_css_class("compact")
            empty.add(status)
            self._page.add(empty)
            return
        several = len({e["playlist_id"] for e in items}) > 1
        # One section per part of the app: Live TV, Movies, Series.
        for media_type in MediaType:
            section = [e for e in items if e["media_type"] is media_type]
            if not section:
                continue
            group = Adw.PreferencesGroup(title=media_type.label)
            for entry in sorted(section, key=lambda e: (e["kind"] != "group",
                                                        e["name"].casefold())):
                group.add(self._row(entry, several))
            self._page.add(group)

    def _row(self, entry: dict, several: bool) -> Gtk.Widget:
        if entry["kind"] == "group":
            what = _("Group") if entry["media_type"] is MediaType.LIVE else _("Category")
            parts = [what]
            icon = Gtk.Image(icon_name="folder-symbolic", pixel_size=24)
        else:
            parts = [_("Channel"), entry["group"]]
            icon = Gtk.Image(pixel_size=24)
            icon.add_css_class("ashy-logo-badge")
            set_logo(icon, entry["logo"], 24, "tv-symbolic")
        if several:
            parts.append(entry["playlist"])
        row = Adw.ActionRow(title=GLib.markup_escape_text(entry["name"]),
                            subtitle=GLib.markup_escape_text(
                                " • ".join(p for p in parts if p)))
        row.add_prefix(icon)
        show = Gtk.Button(label=_("Show"), valign=Gtk.Align.CENTER)
        show.connect("clicked", lambda _b, e=entry["entries"]: self._apply(e))
        row.add_suffix(show)
        return row

    def _groups(self):
        # Adw.PreferencesPage has no API to list its groups: walk the widgets.
        stack = [self._page]
        while stack:
            widget = stack.pop()
            child = widget.get_first_child()
            while child is not None:
                if isinstance(child, Adw.PreferencesGroup):
                    yield child
                else:
                    stack.append(child)
                child = child.get_next_sibling()

    def _apply(self, entries: list | None) -> None:
        if entries is None:
            self.storage.unhide_all()
        else:
            self.storage.unhide(entries)
        self._on_changed()
        self._fill()
