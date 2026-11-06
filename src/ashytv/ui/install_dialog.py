"""Window shown instead of the main one when the player library is missing.

Runs the distro's package manager through pkexec and streams its output into
a text view (no terminal widget: the commands run without prompts)."""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib, Gtk

from ..i18n import _


class InstallLibmpvWindow(Adw.ApplicationWindow):
    def __init__(self, plan: dict | None, **kwargs):
        super().__init__(title="AshyTV", default_width=640, default_height=520, **kwargs)
        self._plan = plan
        self._proc: Gio.Subprocess | None = None
        # Finish only after the process exited AND its output was drained
        self._exit_ok: bool | None = None
        self._output_done = False

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self.set_content(toolbar)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=18, margin_start=18, margin_end=18)
        toolbar.set_content(box)

        group = Adw.PreferencesGroup(
            title=_("Video player library not found"),
            description=_("AshyTV plays video with libmpv, which is not installed on "
                          "this system. It is installed once from your distribution's "
                          "packages."))
        box.append(group)

        if plan is None:
            group.add(Adw.ActionRow(
                title=_("Install it with your package manager"),
                subtitle=_("Install the package that provides libmpv.so.2 "
                           "(usually “mpv”, “libmpv2” or “mpv-libs”) and open "
                           "AshyTV again."),
                subtitle_lines=4))
        else:
            group.add(Adw.ActionRow(title=_("Command to be executed"),
                                    subtitle=plan["display"], subtitle_selectable=True))

        self._buffer = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=self._buffer, editable=False, cursor_visible=False,
                            monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                            top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        self._view = view
        self._end = self._buffer.create_mark(None, self._buffer.get_end_iter(), False)
        scroller = Gtk.ScrolledWindow(child=view, vexpand=True, min_content_height=180,
                                      visible=False)
        scroller.add_css_class("card")
        self._scroller = scroller
        box.append(scroller)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        box.append(buttons)
        self._close = Gtk.Button(label=_("Close"))
        self._close.connect("clicked", lambda *_: self.close())
        buttons.append(self._close)
        self._install = Gtk.Button(label=_("Install"), visible=plan is not None)
        self._install.add_css_class("suggested-action")
        self._install.connect("clicked", self._on_install)
        buttons.append(self._install)

        self.connect("close-request", lambda *_: self._proc is not None)

    def _append(self, text: str) -> None:
        self._buffer.insert(self._buffer.get_end_iter(), text)
        self._view.scroll_mark_onscreen(self._end)

    def _on_install(self, *_args) -> None:
        self._install.set_sensitive(False)
        self._close.set_sensitive(False)
        self._scroller.set_visible(True)
        self._append(f"$ {self._plan['display']}\n")
        self._exit_ok, self._output_done = None, False
        try:
            self._proc = Gio.Subprocess.new(
                self._plan["command"],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as exc:
            self._finish(False, exc.message)
            return
        stream = Gio.DataInputStream.new(self._proc.get_stdout_pipe())
        self._read_line(stream)
        self._proc.wait_check_async(None, self._on_exited)

    def _read_line(self, stream: Gio.DataInputStream) -> None:
        def done(source, result):
            try:
                line, _length = source.read_line_finish_utf8(result)
            except GLib.Error:
                line = None
            if line is None:
                self._output_done = True
                self._maybe_finish()
                return
            self._append(line + "\n")
            self._read_line(source)
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, done)

    def _on_exited(self, proc: Gio.Subprocess, result) -> None:
        try:
            ok = proc.wait_check_finish(result)
        except GLib.Error:
            ok = False
        self._exit_ok = ok
        self._maybe_finish()

    def _maybe_finish(self) -> None:
        if self._exit_ok is None or not self._output_done:
            return
        self._proc = None
        self._finish(self._exit_ok, "")

    def _finish(self, ok: bool, error: str) -> None:
        if error:
            self._append(error + "\n")
        if ok:
            self._append("\n" + _("Installed. Open AshyTV again to start watching.") + "\n")
            self._close.set_label(_("Close AshyTV"))
            self._close.add_css_class("suggested-action")
        else:
            self._append("\n" + _("The installation did not finish. Check the "
                                  "messages above and try again.") + "\n")
            self._install.set_sensitive(True)
        self._close.set_sensitive(True)
