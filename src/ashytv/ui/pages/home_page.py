"""Home page — featured carousel, continue watching and poster rails
(the newest titles, then one rail per playlist group)."""

from __future__ import annotations

import time

from gi.repository import Adw, GLib, GObject, Gtk

from ...i18n import _
from ...models import Channel, MediaType
from ...services import home
from ...services.storage import collapse_variants
from ..marquee import MarqueeLabel
from .. import keynav
from ._common import clear_box, set_logo, set_picture
from .catalog_page import chip_label

_POSTER_W, _POSTER_H = 130, 195

_HERO_H = 300
_SLIDE_SECS = 7


class RailCards:
    """Titled horizontal rails of channel and poster cards (Home, Favorites).
    The class using it emits "play" (a channel; a movie or series opens its
    details) and "open-group", and may set `_guide` for what is on now."""

    _guide = None

    def _section(self, parent, title, link=None, target=None):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(10)
        head = Gtk.Box()
        lbl = Gtk.Label(label=title, xalign=0, hexpand=True)
        lbl.add_css_class("ashy-section-title")
        head.append(lbl)
        if link:
            # For the mouse: with the keyboard ▼ goes rail to rail (this link,
            # at the right end of the title, pulled the focus to the edge) and
            # the menu opens the section.
            btn = Gtk.Button(label=link, focusable=False)
            btn.add_css_class("flat")
            btn.connect("clicked", lambda *_: self.emit("navigate", target))
            head.append(btn)
        box.append(head)
        # Room under the cards for the overlay scrollbar, so it never covers them.
        row = Gtk.Box(spacing=14, margin_bottom=10)
        sw = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(row)
        box.append(sw)
        parent.append(box)
        return box, row

    @staticmethod
    def _fill(box, row, items, make) -> None:
        clear_box(row)
        for item in items:
            row.append(make(item))
        box.set_visible(bool(items))

    # ---- cards ---------------------------------------------------------

    def _channel_card(self, ch: Channel) -> Gtk.Widget:
        card = Gtk.Button()
        card.add_css_class("ashy-card")
        card.set_size_request(150, 120)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=14, margin_bottom=12, margin_start=10, margin_end=10)
        img = Gtk.Image(pixel_size=52)
        set_logo(img, ch.logo, 52, "tv-symbolic")
        name = Gtk.Label(label=ch.display_name, ellipsize=3, max_width_chars=16)
        name.add_css_class("caption")
        box.append(img)
        box.append(name)
        if self._guide and ch.tvg_id:
            cur, _n = self._guide.now_next(ch.tvg_id)
            if cur:
                p = Gtk.Label(label=cur.title, ellipsize=3, max_width_chars=16)
                p.add_css_class("ashy-faint")
                box.append(p)
        card.set_child(box)
        card.connect("clicked", lambda *_: self.emit("play", ch))
        card.ashy_key = (ch.playlist_id, ch.url)  # found again after a rebuild
        return card

    def _poster_card(self, ch: Channel) -> Gtk.Widget:
        # Not expanding (the year label inside does): in a short rail the
        # card took the spare width and stretched the poster frame.
        card = Gtk.Button(hexpand=False)
        card.add_css_class("flat")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        frame = Gtk.Box()
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        frame.add_css_class("ashy-poster-img")
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        pic.set_size_request(_POSTER_W, _POSTER_H)
        set_picture(pic, ch.logo, _POSTER_H)
        frame.append(pic)
        box.append(frame)
        t = MarqueeLabel(width_chars=14)
        t.set_text(ch.display_name)
        t.add_css_class("ashy-poster-title")
        t.attach_hover(card)
        t.attach_focus(card)
        box.append(t)
        meta = Gtk.Box()
        y = Gtk.Label(label=ch.year, xalign=0, hexpand=True)
        y.add_css_class("ashy-year")
        r = Gtk.Label(label=f"★ {ch.rating:.1f}" if ch.rating else "")
        r.add_css_class("ashy-rating")
        meta.append(y)
        meta.append(r)
        box.append(meta)
        card.set_child(box)
        card.connect("clicked", lambda *_: self.emit("play", ch))
        card.ashy_key = (ch.playlist_id, ch.url)  # found again after a rebuild
        return card

    def _more_card(self, media_type: MediaType, chip: str) -> Gtk.Widget:
        """Poster-sized "See more" at the end of a rail: Movies or Series on
        that group (or on the recently added)."""
        card = Gtk.Button(valign=Gtk.Align.START,
                          tooltip_text=_("See more in {section}").format(
                              section=media_type.label))
        card.add_css_class("flat")
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                        valign=Gtk.Align.CENTER)
        frame.set_size_request(_POSTER_W, _POSTER_H)
        frame.add_css_class("ashy-poster-img")
        frame.add_css_class("ashy-more-card")
        icon = Gtk.Image(icon_name="list-add-symbolic", pixel_size=40,
                         vexpand=True, valign=Gtk.Align.END)
        label = Gtk.Label(label=_("See more"), vexpand=True, valign=Gtk.Align.START)
        label.add_css_class("ashy-poster-title")
        frame.append(icon)
        frame.append(label)
        card.set_child(frame)
        card.connect("clicked", lambda *_: self.emit("open-group", media_type.value, chip))
        return card


class HomePage(RailCards, Gtk.Box):
    __gtype_name__ = "AshyHomePage"

    __gsignals__ = {
        # a channel to watch; a movie or series opens its details
        "play": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # a movie to play right away (the featured box's Watch)
        "watch": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # "See more": Movies or Series (media type value) on a chip
        # (a group, or "new" for the recently added)
        "open-group": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # history item (sqlite3.Row) to resume
        "resume": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "navigate": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, storage):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.storage = storage
        self._guide = None
        self._dirty = True

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        inner.add_css_class(keynav.RAILS_CLASS)
        inner.set_margin_top(10)
        inner.set_margin_start(28)
        inner.set_margin_end(28)
        inner.set_margin_bottom(24)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(inner)
        self.append(sw)

        # ---- carousel ----
        # The whole banner is one item, as on the TV apps: ← → go through the
        # titles (← on the first one: the menu), Enter or a click opens it; with
        # the mouse, the arrows at its sides.
        self.carousel = Adw.Carousel(allow_scroll_wheel=False, allow_mouse_drag=False,
                                     spacing=12)
        self.carousel.set_size_request(-1, _HERO_H)
        self.hero = Gtk.Button(child=self.carousel, focus_on_click=False)
        self.hero.add_css_class("ashy-hero-btn")
        self.hero.connect("clicked", lambda *_a: self._open_slide())
        hero = Gtk.Overlay(child=self.hero)
        hero.add_css_class("ashy-hero-box")
        dots = Adw.CarouselIndicatorDots(carousel=self.carousel,
                                         halign=Gtk.Align.CENTER,
                                         valign=Gtk.Align.END, margin_bottom=8)
        hero.add_overlay(dots)
        self._arrows: list[Gtk.Button] = []
        for icon, step, align in (("go-previous-symbolic", -1, Gtk.Align.START),
                                  ("go-next-symbolic", 1, Gtk.Align.END)):
            arrow = Gtk.Button(icon_name=icon, focusable=False, halign=align,
                               valign=Gtk.Align.CENTER, margin_start=12, margin_end=12)
            arrow.add_css_class("ashy-hero-arrow")
            arrow.add_css_class("circular")
            arrow.connect("clicked", lambda _b, d=step: self.hero_step(d, wrap=True))
            hero.add_overlay(arrow)
            self._arrows.append(arrow)
        inner.append(hero)
        self._slides: list = []
        self.carousel.connect("page-changed", lambda _c, index: self._on_slide(index))
        GLib.timeout_add_seconds(_SLIDE_SECS, self._advance)

        # ---- sections ----
        self.continue_box, self.continue_row = self._section(inner, _("Continue Watching"))
        self.live_box, self.live_row = self._section(inner, _("Live Channels"),
                                                     _("See all"), "channels")
        self.movies_box, self.movies_row = self._section(inner, _("Recently Added Movies"),
                                                         _("See all"), "movies")
        self.series_box, self.series_row = self._section(inner, _("Recently Added Series"),
                                                         _("See all"), "series")
        # One rail per group of the playlists (rebuilt with the catalog).
        self.groups_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        inner.append(self.groups_box)

    # ---- API -----------------------------------------------------------

    def set_guide(self, guide) -> None:
        self._guide = guide
        self.invalidate()

    def mark_stale(self) -> None:
        """Refresh on the next visit, not now."""
        self._dirty = True

    def favorite_changed(self, _channel) -> None:
        self.invalidate()

    def invalidate(self) -> None:
        self._dirty = True
        if self.get_mapped():
            self.refresh()

    def refresh(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        self._build_hero()
        self._build_continue()
        self._fill(self.live_box, self.live_row,
                   self._live_channels(16), self._channel_card)
        for mt, box, row in ((MediaType.MOVIE, self.movies_box, self.movies_row),
                             (MediaType.SERIES, self.series_box, self.series_row)):
            items = self.storage.channels(mt, order="added", limit=home.RAIL_TITLES + 1)
            self._fill_rail(box, row, items, mt, "new")
        self._build_group_rails()

    def _fill_rail(self, box, row, items, media_type: MediaType, chip: str) -> None:
        """Up to RAIL_TITLES posters, then "See more" when there are more."""
        shown, more = home.rail(items)
        self._fill(box, row, shown, self._poster_card)
        if more:
            row.append(self._more_card(media_type, chip))

    def _build_group_rails(self) -> None:
        clear_box(self.groups_box)
        rails = home.group_rails(self.storage.groups(MediaType.MOVIE),
                                 self.storage.groups(MediaType.SERIES))
        for mt, group, count in rails:
            box, row = self._section(self.groups_box, chip_label(group))
            items = self.storage.channels(mt, group=group, limit=home.RAIL_TITLES)
            self._fill(box, row, items, self._poster_card)
            if count > home.RAIL_TITLES:
                row.append(self._more_card(mt, group))

    def _live_channels(self, limit: int) -> list[Channel]:
        chans = self.storage.channels(MediaType.LIVE, limit=limit * 4)
        if self.storage.group_variants:
            chans = collapse_variants(chans)
        return chans[:limit]

    # ---- hero ----------------------------------------------------------

    def _build_hero(self) -> None:
        while self.carousel.get_n_pages():
            self.carousel.remove(self.carousel.get_nth_page(0))
        # The newest titles with a picture, movies and series in turns.
        featured = home.featured(
            self.storage.channels(MediaType.MOVIE, order="added", limit=40),
            self.storage.channels(MediaType.SERIES, order="added", limit=40))
        self._slides = list(featured)
        for arrow in self._arrows:
            arrow.set_visible(len(featured) > 1)
        if not featured:
            self.carousel.append(self._welcome_slide())
            return
        for ch in featured:
            self.carousel.append(self._slide(ch, len(featured) > 1))
        self._on_slide(0)

    def _welcome_slide(self) -> Gtk.Widget:
        slide = Gtk.Overlay(hexpand=True)
        bg = Gtk.Box()
        bg.add_css_class("ashy-hero-fallback")
        slide.set_child(bg)
        slide.add_overlay(self._hero_text(
            _("Welcome to AshyTV"),
            _("Your channels, movies and series in one place"),
            _("Enter: view channels")))
        return slide

    def _slide(self, ch: Channel, several: bool) -> Gtk.Widget:
        slide = Gtk.Overlay(hexpand=True)
        slide.add_css_class("ashy-hero")
        slide.set_overflow(Gtk.Overflow.HIDDEN)
        base = Gtk.Box()
        base.set_size_request(-1, _HERO_H)
        base.add_css_class("ashy-hero-fallback")
        slide.set_child(base)
        bg = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        set_picture(bg, ch.backdrop or ch.logo, 780)
        slide.add_overlay(bg)  # overlay: doesn't change the hero height
        shade = Gtk.Box()
        shade.add_css_class("ashy-hero-shade")
        slide.add_overlay(shade)
        if not ch.backdrop:
            # No backdrop: sharp poster on the right over the enlarged background.
            frame = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.CENTER,
                            margin_end=40)
            frame.set_overflow(Gtk.Overflow.HIDDEN)
            frame.add_css_class("ashy-poster-img")
            poster = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
            poster.set_size_request(160, 240)
            set_picture(poster, ch.logo, 240)
            frame.append(poster)
            slide.add_overlay(frame)
        meta = "  •  ".join(p for p in (
            ch.media_type.label, ch.year,
            f"★ {ch.rating:.1f}" if ch.rating else "", ch.genre) if p)
        hint = _("Enter: details") + ("  ·  " + _("← → more titles") if several else "")
        slide.add_overlay(self._hero_text(ch.display_name, meta, hint, ch.plot))
        return slide

    def _hero_text(self, title, sub, hint, plot="") -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      valign=Gtk.Align.CENTER, halign=Gtk.Align.START)
        box.set_margin_start(36)
        box.set_size_request(-1, -1)
        t = Gtk.Label(label=title, xalign=0, wrap=True, max_width_chars=28, lines=2,
                      ellipsize=3)
        t.add_css_class("ashy-hero-title")
        s = Gtk.Label(label=sub, xalign=0, wrap=True, max_width_chars=50)
        s.add_css_class("ashy-hero-sub")
        box.append(t)
        box.append(s)
        if plot:
            p = Gtk.Label(label=plot, xalign=0, wrap=True, max_width_chars=60,
                          lines=2, ellipsize=3)
            p.add_css_class("ashy-faint")
            box.append(p)
        h = Gtk.Label(label=hint, xalign=0, margin_top=6)
        h.add_css_class("ashy-hero-hint")
        box.append(h)
        return box

    def focus_content(self) -> bool:
        """Entering Home (and opening the app) lands on the banner."""
        return self.hero.grab_focus()

    def hero_has_focus(self, widget) -> bool:
        return widget is self.hero

    def _index(self) -> int:
        return round(self.carousel.get_position())

    def hero_step(self, step: int, wrap: bool = False) -> bool:
        """← / →: the previous / next title. False on ← at the first one
        (the focus goes on to the menu)."""
        n = self.carousel.get_n_pages()
        if n < 2:
            return False
        i = self._index() + step
        if i < 0 and not wrap:
            return False
        i %= n
        self._moved_at = GLib.get_monotonic_time()
        self.carousel.scroll_to(self.carousel.get_nth_page(i), True)
        return True

    def _open_slide(self) -> None:
        if not self._slides:
            self.emit("navigate", "channels")
            return
        self.emit("play", self._slides[min(self._index(), len(self._slides) - 1)])

    def _on_slide(self, index: int) -> None:
        for i in range(self.carousel.get_n_pages()):
            self.carousel.get_nth_page(i).set_can_focus(False)

    def _advance(self) -> bool:
        n = self.carousel.get_n_pages()
        # A title picked by hand stays a full turn before the next one.
        recent = GLib.get_monotonic_time() - getattr(self, "_moved_at", 0) < _SLIDE_SECS * 1_000_000
        if n > 1 and self.get_mapped() and not recent:
            nxt = (self._index() + 1) % n
            self.carousel.scroll_to(self.carousel.get_nth_page(nxt), True)
        return GLib.SOURCE_CONTINUE

    # ---- continue watching ------------------------------------------

    def _build_continue(self) -> None:
        clear_box(self.continue_row)
        rows = self.storage.history(16)
        for r in rows:
            self.continue_row.append(self._history_card(r))
        self.continue_box.set_visible(bool(rows))

    def _history_card(self, r) -> Gtk.Widget:
        card = Gtk.Button()
        card.add_css_class("flat")
        card.connect("clicked", lambda *_: self.emit("resume", r))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        mt = MediaType(r["media_type"])
        thumb = Gtk.Overlay()
        thumb.set_size_request(200, 112)
        thumb.set_overflow(Gtk.Overflow.HIDDEN)
        thumb.add_css_class("ashy-thumb")
        if mt is MediaType.LIVE:
            img = Gtk.Image(pixel_size=56, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            set_logo(img, r["logo"], 56, "tv-symbolic")
            thumb.set_child(img)
        else:
            pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
            set_picture(pic, r["logo"], 225)
            thumb.set_child(pic)
        box.append(thumb)

        title = Gtk.Label(label=r["title"], xalign=0, ellipsize=3, max_width_chars=22)
        title.add_css_class("ashy-poster-title")
        box.append(title)

        sub, frac = r["subtitle"] or "", None
        if mt is MediaType.LIVE:
            if self._guide and r["tvg_id"]:
                cur, _n = self._guide.now_next(r["tvg_id"])
                if cur:
                    sub = (f"{time.strftime('%H:%M', time.localtime(cur.start))} – "
                           f"{time.strftime('%H:%M', time.localtime(cur.stop))}  {cur.title}")
                    frac = cur.progress(int(time.time()))
        elif r["duration"]:
            frac = min(1.0, r["position"] / r["duration"])
        s = Gtk.Label(label=sub, xalign=0, ellipsize=3, max_width_chars=26)
        s.add_css_class("ashy-faint")
        box.append(s)
        if frac is not None:
            bar = Gtk.ProgressBar(fraction=frac)
            bar.add_css_class("ashy-progress-mini")
            box.append(bar)
        card.set_child(box)
        return card
