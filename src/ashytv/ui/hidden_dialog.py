"""Settings → Hidden channels and groups: show them again, per playlist."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk

from ..i18n import _
from ..models import MediaType
from ..services.quality import split_quality


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
        rows = self.storage.hidden_items()
        self._show_all.set_sensitive(bool(rows))
        if not rows:
            empty = Adw.PreferencesGroup()
            status = Adw.StatusPage(icon_name="view-reveal-symbolic",
                                    title=_("Nothing hidden"),
                                    description=_("Right-click (or long-press) a group or "
                                                  "channel to hide it."))
            status.add_css_class("compact")
            empty.add(status)
            self._page.add(empty)
            return
        # Quality variants of one channel ("News FHD", "News HD") are one row.
        merged: dict[tuple, tuple[str, list]] = {}
        for r in rows:
            name = r["label"] or r["key"]
            if r["kind"] == "channel":
                name = split_quality(name)[0] or name
            key = (r["playlist_id"], r["playlist"], r["kind"], r["media_type"], name)
            merged.setdefault(key, (name, []))[1].append(
                (r["playlist_id"], r["kind"], r["media_type"], r["key"]))
        groups: dict[int, Adw.PreferencesGroup] = {}
        for (pid, playlist, kind, media_type, _n), (name, entries) in merged.items():
            group = groups.get(pid)
            if group is None:
                group = groups[pid] = Adw.PreferencesGroup(
                    title=GLib.markup_escape_text(playlist))
                self._page.add(group)
            what = _("Group") if kind == "group" else _("Channel")
            row = Adw.ActionRow(title=GLib.markup_escape_text(name),
                                subtitle=f"{what} • {MediaType(media_type).label}")
            show = Gtk.Button(label=_("Show"), valign=Gtk.Align.CENTER)
            show.connect("clicked", lambda _b, e=entries: self._apply(e))
            row.add_suffix(show)
            group.add(row)

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
