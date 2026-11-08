"""Recordings page: what is scheduled, and the files already recorded
(by the player through mpv, or by a scheduled recording)."""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ...i18n import _
from ...models import Channel, MediaType
from ...services import recordings
from ._common import page_header
from .settings_page import recordings_dir

_EXTS = {".ts", ".mkv", ".mp4"}


class RecordingsPage(Gtk.Box):
    __gtype_name__ = "AshyRecordingsPage"

    __gsignals__ = {
        "play": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # key of a scheduled recording the user no longer wants
        "cancel-scheduled": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.recordings = None  # RecordingBook, set by the window
        head = Gtk.Box()
        head.append(page_header(_("Recordings"),
                                _("Use the ● button in the player, or schedule one "
                                  "from the guide")))
        head.get_first_child().set_hexpand(True)
        folder = Gtk.Button(icon_name="folder-videos-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text=_("Open folder"), margin_end=28)
        folder.add_css_class("flat")
        folder.connect("clicked", self._open_folder)
        head.append(folder)
        self.append(head)

        # What is scheduled sits above what is already recorded: it is the
        # part someone comes here to check or call off.
        self.scheduled = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                     valign=Gtk.Align.START, visible=False)
        self.scheduled.add_css_class("boxed-list")
        self.scheduled_title = Gtk.Label(label=_("Scheduled"), xalign=0, visible=False,
                                         margin_bottom=6)
        self.scheduled_title.add_css_class("heading")
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                valign=Gtk.Align.START)
        self.list.add_css_class("boxed-list")
        self.list.connect("row-activated", self._on_row)
        self.empty = Adw.StatusPage(icon_name="ashy-record-symbolic",
                                    title=_("No recordings"),
                                    description=_("Recordings appear here."))
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        inner.append(self.scheduled_title)
        inner.append(self.scheduled)
        inner.append(self.list)
        clamp = Adw.Clamp(maximum_size=900, child=inner, margin_start=28,
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
        self._refresh_scheduled()
        self.list.remove_all()
        folder = recordings_dir()
        # One stat() per file, and a file deleted meanwhile is just skipped.
        entries = []
        for f in folder.glob("*") if folder.exists() else ():
            if f.suffix.lower() not in _EXTS:
                continue
            try:
                entries.append((f, f.stat()))
            except OSError:
                continue
        # A recording in progress is already shown above, as "Recording now".
        # Its file is still being written, so down here it would appear a
        # second time, with whatever size happened to be on disk.
        running = {s.path for s in (self.recordings or ())
                   if s.state == recordings.RECORDING and s.path}
        entries = [e for e in entries if str(e[0]) not in running]
        entries.sort(key=lambda e: e[1].st_mtime, reverse=True)
        files = [f for f, _st in entries]
        for f, st in entries:
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
        # The empty page only when there is nothing of either kind.
        self.stack.set_visible_child_name(
            "list" if files or self.scheduled.get_visible() else "empty")

    def _refresh_scheduled(self) -> None:
        self.scheduled.remove_all()
        book = self.recordings
        items = [s for s in book] if book is not None else []
        for s in items:
            when = GLib.DateTime.new_from_unix_local(s.start).format("%x %R")
            if s.state == recordings.RECORDING:
                note = _("Recording now")
            elif s.state == recordings.DONE:
                note = _("Recorded")
            elif s.state == recordings.FAILED:
                note = s.error or _("Failed")
            else:
                note = _("Waiting")
            row = Adw.ActionRow(title=GLib.markup_escape_text(s.title),
                                subtitle=f"{GLib.markup_escape_text(s.channel_name)} • {when} • {note}")
            icon = ("media-record-symbolic" if s.state == recordings.RECORDING
                    else "dialog-warning-symbolic" if s.state == recordings.FAILED
                    else "alarm-symbolic")
            row.add_prefix(Gtk.Image(icon_name=icon))
            if s.state == recordings.RECORDING:
                row.get_first_child().add_css_class("recording")
            drop = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text=_("Stop recording") if s.state == recordings.RECORDING
                              else _("Cancel"))
            drop.add_css_class("flat")
            drop.connect("clicked", lambda _b, k=s.key: self.emit("cancel-scheduled", k))
            row.add_suffix(drop)
            self.scheduled.append(row)
        self.scheduled.set_visible(bool(items))
        self.scheduled_title.set_visible(bool(items))

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
