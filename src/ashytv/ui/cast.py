"""Cast of movies/series and actor pages (Jellyfin-like).

`CastService` talks to the META API (services/meta.py) in worker threads and
falls back to the provider's text cast. `CastRow` is the row of actor cards
in the detail dialog; `ActorDialog` lists the person's titles that are in the
user's own catalog.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable

from gi.repository import Adw, GLib, GObject, Gtk

from ..i18n import _
from ..models import Channel, MediaType
from ..services import languages, meta
from ..services.meta import CastMember, CatalogIndex, MetaError, Person
from ..services.storage import Storage
from .marquee import MarqueeLabel
from .pages._common import set_picture

_YEAR_SUFFIX_RE = re.compile(r"(?:\((?:19|20)\d{2}\)|-[ \t]?(?:19|20)\d{2})$")
_FAINT = "ashy-faint"


def search_title(name: str) -> str:
    """'[L] Duna: Parte Dois - 2024 [4K]' -> 'Duna: Parte Dois' (the query sent)."""
    name = name.strip()
    while True:
        if name.endswith("]") and "[" in name:
            cut = name[:name.rfind("[")].strip()
        elif name.startswith("[") and "]" in name:  # "[L] Title"
            cut = name[name.find("]") + 1:].strip()
        else:
            cut = _YEAR_SUFFIX_RE.sub("", name).strip()
        if not cut or cut == name:
            return name
        name = cut


def _lang() -> str:
    codes = languages.preferred_codes()
    return codes[0] if codes else ""


class CastService:
    """Cast and filmographies, off the main thread. Callbacks run on it."""

    def __init__(self, storage: Storage, client: meta.MetaClient | None = None):
        self.storage = storage
        self.client = client or meta.MetaClient()
        # (playlist_id, url) -> cast text / TMDB id from the provider details
        # opened in this session (the fallback when the META API can't help).
        self.cast_texts: dict[tuple[int, str], str] = {}
        self.tmdb_ids: dict[tuple[int, str], str] = {}
        self._index: CatalogIndex | None = None
        self._index_key: tuple | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _key(ch: Channel) -> tuple[int, str]:
        return ch.playlist_id, ch.url

    def remember(self, ch: Channel, cast_text: str = "", tmdb_id: str = "") -> None:
        if cast_text:
            self.cast_texts[self._key(ch)] = cast_text
        if tmdb_id:
            self.tmdb_ids[self._key(ch)] = tmdb_id

    # ---- cast of a title ---------------------------------------------------

    def load_cast(self, ch: Channel, info: dict,
                  on_done: Callable[[list[CastMember], bool], None]) -> None:
        """on_done(members, from_tmdb) — an empty list when there is nothing."""
        key = self._key(ch)
        tmdb = info.get("tmdb_id") or self.tmdb_ids.get(key) or ch.tmdb
        text = info.get("cast") or self.cast_texts.get(key, "")
        if info.get("cast"):
            self.remember(ch, info["cast"], info.get("tmdb_id", ""))
        kind = MediaType.SERIES if ch.media_type is MediaType.SERIES else MediaType.MOVIE
        query, year, lang = search_title(ch.display_name), meta.channel_year(ch), _lang()

        def worker():
            try:
                members = self.client.title(kind, tmdb_id=tmdb, query=query, year=year,
                                            lang=lang)
            except MetaError:
                members = []
            if members:
                GLib.idle_add(_call, on_done, members, True)
            else:
                GLib.idle_add(_call, on_done, meta.split_cast(text), False)

        threading.Thread(target=worker, daemon=True, name="ashytv-cast").start()

    # ---- people by name ------------------------------------------------------

    def search_people(self, query: str,
                      on_done: Callable[[str, list[CastMember]], None]) -> None:
        """on_done(query, people) — [] when the panel can't help."""
        lang = _lang()

        def worker():
            try:
                people = self.client.people(query, lang)
            except MetaError:
                people = []
            GLib.idle_add(_call, on_done, query, people)

        threading.Thread(target=worker, daemon=True, name="ashytv-people").start()

    # ---- a person's titles -------------------------------------------------

    def load_person(self, member: CastMember,
                    on_done: Callable[[Person, list[Channel], list[Channel], bool], None]
                    ) -> None:
        """on_done(person, movies, series, from_tmdb) with only the titles the
        user has. Without TMDB data: the titles whose opened details list
        the name."""
        index_key = self._catalog_key()
        cast_texts = dict(self.cast_texts)
        lang = _lang()

        def worker():
            person = None
            if member.id:
                try:
                    person = self.client.person(member.id, lang)
                except MetaError:
                    person = None
            try:
                index = self._catalog_index(index_key)
            except Exception:  # the database is busy or gone: show no titles
                index = CatalogIndex(())
            from_tmdb = person is not None
            if person is not None:
                movies, series = index.match(person.credits)
                if not person.profile:
                    person.profile = member.profile
            else:
                person = Person(name=member.name, id=member.id, profile=member.profile)
                movies, series = meta.titles_with_cast(member.name, cast_texts,
                                                       index.by_key.get)
            GLib.idle_add(_call, on_done, person, movies, series, from_tmdb)

        threading.Thread(target=worker, daemon=True, name="ashytv-person").start()

    def _catalog_key(self) -> tuple:
        """Changes when a playlist is updated or the filters change."""
        s = self.storage
        return (tuple((r["id"], r["updated_at"]) for r in s.list_playlists()),
                s.hide_adult, s.hidden_version, frozenset(s.disabled))

    def _catalog_index(self, key: tuple) -> CatalogIndex:
        with self._lock:
            if self._index is not None and self._index_key == key:
                return self._index
            # Its own connection: sqlite connections are per thread.
            db = Storage(self.storage.path, timeout=5)
            try:
                db.hide_adult = self.storage.hide_adult
                db.disabled = set(self.storage.disabled)
                items = db.channels(MediaType.MOVIE) + db.channels(MediaType.SERIES)
            finally:
                db.close()
            self._index, self._index_key = CatalogIndex(items), key
            return self._index


def _call(callback, *args) -> bool:
    callback(*args)
    return GLib.SOURCE_REMOVE


# ---- widgets ------------------------------------------------------------------

_PHOTO_W, _PHOTO_H = 104, 156
# TMDB "known_for" departments of people found by name.
_ROLES = {"Acting": _("Actor"), "Directing": _("Director"), "Writing": _("Writer"),
          "Production": _("Producer")}


def _photo(url: str, name: str, width: int, height: int) -> Gtk.Widget:
    """Portrait photo; the initials while loading or when there is none."""
    frame = Gtk.Overlay()
    frame.set_size_request(width, height)
    frame.set_overflow(Gtk.Overflow.HIDDEN)
    frame.add_css_class("ashy-cast-photo")
    frame.set_child(Adw.Avatar(size=min(width, height) * 2 // 3, text=name,
                               show_initials=True, halign=Gtk.Align.CENTER,
                               valign=Gtk.Align.CENTER))
    if url:
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        pic.set_size_request(width, height)
        set_picture(pic, url, height)
        frame.add_overlay(pic)
    return frame


class CastRow(Gtk.Box):
    """'Cast' title and a horizontal row of actor cards."""

    __gtype_name__ = "AshyCastRow"

    __gsignals__ = {
        "person": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, title: str = ""):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=False)
        title = Gtk.Label(label=title or _("Cast"), xalign=0)
        title.add_css_class("ashy-section-title")
        self.append(title)
        self._cards = Gtk.Box(spacing=12, margin_bottom=10)
        scroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER,
                                    hscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self._cards)
        self.append(scroll)

    def set_cast(self, members: list[CastMember]) -> None:
        while (child := self._cards.get_first_child()) is not None:
            self._cards.remove(child)
        for member in members:
            self._cards.append(self._card(member))
        self.set_visible(bool(members))

    def _card(self, member: CastMember) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(_photo(member.profile, member.name, _PHOTO_W, _PHOTO_H))
        name = Gtk.Label(label=member.name, wrap=True, lines=2, ellipsize=3,
                         justify=Gtk.Justification.CENTER, max_width_chars=12,
                         width_chars=12)
        name.add_css_class("ashy-cast-name")
        box.append(name)
        if member.character:
            role = Gtk.Label(label=_ROLES.get(member.character, member.character),
                             wrap=True, lines=2, ellipsize=3,
                             justify=Gtk.Justification.CENTER, max_width_chars=12,
                             width_chars=12)
            role.add_css_class(_FAINT)
            box.append(role)
        card = Gtk.Button(child=box, valign=Gtk.Align.START,
                          tooltip_text=_("Titles with {name}").format(name=member.name))
        card.add_css_class("flat")
        card.add_css_class("ashy-cast-card")
        card.connect("clicked", lambda *_: self.emit("person", member))
        return card


class ActorDialog(Adw.Dialog):
    """Photo, name and short biography, then the person's movies and series
    that are in the user's playlists."""

    __gtype_name__ = "AshyActorDialog"

    __gsignals__ = {
        "open": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, member: CastMember):
        super().__init__(title=member.name, content_width=880, content_height=640)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.add_css_class("flat")
        view.add_top_bar(header)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        page.set_margin_start(28)
        page.set_margin_end(28)
        page.set_margin_bottom(24)
        top = Gtk.Box(spacing=22)
        self._photo_slot = Gtk.Box()
        self._photo_slot.append(_photo(member.profile, member.name, 140, 210))
        top.append(self._photo_slot)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True,
                        valign=Gtk.Align.CENTER)
        name = Gtk.Label(label=member.name, xalign=0, wrap=True)
        name.add_css_class("ashy-page-title")
        texts.append(name)
        self._bio = Gtk.Label(xalign=0, wrap=True, lines=8, ellipsize=3, visible=False)
        self._bio.add_css_class("ashy-page-subtitle")
        texts.append(self._bio)
        top.append(texts)
        page.append(top)

        self._spinner = Adw.Spinner(height_request=32, width_request=32,
                                    halign=Gtk.Align.CENTER)
        page.append(self._spinner)
        self._sections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        page.append(self._sections)
        self._credit = Gtk.Label(label=_("Cast data: TMDB"), xalign=0, visible=False)
        self._credit.add_css_class(_FAINT)
        page.append(self._credit)

        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(page)
        view.set_content(scroll)
        self.set_child(view)
        self._member = member

    def show_person(self, person: Person, movies: list[Channel], series: list[Channel],
                    from_tmdb: bool) -> None:
        self._spinner.set_visible(False)
        if person.profile and person.profile != self._member.profile:
            while (child := self._photo_slot.get_first_child()) is not None:
                self._photo_slot.remove(child)
            self._photo_slot.append(_photo(person.profile, person.name, 140, 210))
        self._bio.set_text(person.biography)
        self._bio.set_tooltip_text(person.biography or None)
        self._bio.set_visible(bool(person.biography))
        for title, items in ((_("Movies"), movies), (_("Series"), series)):
            if items:
                self._sections.append(self._section(title, items))
        if not movies and not series:
            empty = Adw.StatusPage(icon_name="system-search-symbolic",
                                   title=_("Nothing in your playlists"),
                                   description=_("None of this person's movies or series "
                                                 "are in your playlists."))
            empty.add_css_class("compact")
            self._sections.append(empty)
        elif not from_tmdb:
            note = Gtk.Label(label=_("Found in the details of the titles you opened."),
                             xalign=0, wrap=True)
            note.add_css_class(_FAINT)
            self._sections.append(note)
        self._credit.set_visible(from_tmdb)
        # Keyboard: start on the first title (the focus was on the dialog).
        root = self.get_root()
        focus = root.get_focus() if root is not None else None
        if focus is None or not focus.is_ancestor(self._sections):
            self._sections.child_focus(Gtk.DirectionType.TAB_FORWARD)

    def _section(self, title: str, items: list[Channel]) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        label = Gtk.Label(label=f"{title}  ({len(items)})", xalign=0)
        label.add_css_class("ashy-section-title")
        box.append(label)
        # The FlowBox children are the focus/activation targets (one ring
        # each, arrows move in 2D); a button inside them was a second stop.
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                           max_children_per_line=8, min_children_per_line=2,
                           column_spacing=6, row_spacing=6)
        flow.add_css_class("ashy-poster-flow")
        for ch in items:
            child = Gtk.FlowBoxChild(child=self._poster(ch))
            child._channel = ch  # type: ignore[attr-defined]
            child.get_child()._marquee.attach_focus(child)  # type: ignore[attr-defined]
            flow.append(child)
        flow.connect("child-activated",
                     lambda _f, child: self.emit("open", child._channel))
        box.append(flow)
        return box

    def _poster(self, ch: Channel) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        frame = Gtk.Box()
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        frame.add_css_class("ashy-poster-img")
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        pic.set_size_request(120, 180)
        set_picture(pic, ch.logo, 180)
        frame.append(pic)
        box.append(frame)
        title = MarqueeLabel(width_chars=13)
        title.set_text(ch.display_name)
        title.add_css_class("ashy-poster-title")
        box.append(title)
        year = Gtk.Label(label=meta.channel_year(ch), xalign=0)
        year.add_css_class("ashy-year")
        box.append(year)
        # Natural width: a wider FlowBox column must not stretch the poster.
        box.set_halign(Gtk.Align.CENTER)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        title.attach_hover(box)
        box._marquee = title  # type: ignore[attr-defined]
        return box
