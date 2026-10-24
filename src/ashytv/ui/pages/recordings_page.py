"""Recordings page — files recorded by the player (mpv stream-record)."""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ...i18n import _
from ...models import Channel, MediaType
from ._common import page_header
from .settings_page import recordings_dir

_EXTS = {".ts", ".mkv", ".mp4"}


class RecordingsPage(Gtk.Box):
    __gtype_name__ = "AshyRecordingsPage"

    __gsignals__ = {
        "play": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        head = Gtk.Box()
        head.append(page_header(_("Recordings"),
                                _("Use the ● button in the player to record what's on")))
        head.get_first_child().set_hexpand(True)
        folder = Gtk.Button(icon_name="folder-videos-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text=_("Open folder"), margin_end=28)
        folder.add_css_class("flat")
        folder.connect("clicked", self._open_folder)
        head.append(folder)
        self.append(head)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                valign=Gtk.Align.START)
        self.list.add_css_class("boxed-list")
        self.list.connect("row-activated", self._on_row)
        self.empty = Adw.StatusPage(icon_name="ashy-record-symbolic",
                                    title=_("No recordings"),
                                    description=_("Recordings appear here."))
        clamp = Adw.Clamp(maximum_size=900, child=self.list, margin_start=28,
                          margin_end=28, margin_bottom=22)
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                child=clamp)
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(sw, "list")
        self.stack.add_named(self.empty, "empty")
        self.append(self.stack)

    def invalidate(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        self.list.remove_all()
        folder = recordings_dir()
        files = sorted((f for f in folder.glob("*") if f.suffix.lower() in _EXTS),
                       key=lambda f: f.stat().st_mtime, reverse=True) \
            if folder.exists() else []
        for f in files:
            st = f.stat()
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(f.stem),
                subtitle=f"{GLib.DateTime.new_from_unix_local(int(st.st_mtime)).format('%x %R')}"
                         f" • {GLib.format_size(st.st_size)}",
                activatable=True)
            row._path = f  # type: ignore[attr-defined]
            row.add_prefix(Gtk.Image(icon_name="ashy-film-symbolic"))
            delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text=_("Delete recording"))
            delete.add_css_class("flat")
            delete.connect("clicked", self._delete, f)
            row.add_suffix(delete)
            row.add_suffix(Gtk.Image(icon_name="media-playback-start-symbolic"))
            self.list.append(row)
        self.stack.set_visible_child_name("list" if files else "empty")

    def _on_row(self, _list, row) -> None:
        path = row._path  # type: ignore[attr-defined]
        self.emit("play", Channel(name=path.stem, url=str(path),
                                  media_type=MediaType.MOVIE))

    def _delete(self, _b, path) -> None:
        dialog = Adw.AlertDialog(heading=_("Delete recording?"), body=path.name)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_delete_response, path)
        dialog.present(self)

    def _on_delete_response(self, _d, response, path) -> None:
        if response == "delete":
            path.unlink(missing_ok=True)
            self.refresh()

    def _open_folder(self, _b) -> None:
        folder = recordings_dir()
        folder.mkdir(parents=True, exist_ok=True)
        root = self.get_root()
        Gtk.FileLauncher(file=Gio.File.new_for_path(str(folder))).launch(
            root if isinstance(root, Gtk.Window) else None, None, None)
