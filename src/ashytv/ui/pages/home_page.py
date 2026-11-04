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
from ._common import clear_box, set_logo, set_picture
from .catalog_page import chip_label

_POSTER_W, _POSTER_H = 130, 195

_HERO_H = 300
_SLIDE_SECS = 7


class HomePage(Gtk.Box):
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
        inner.set_margin_top(10)
        inner.set_margin_start(28)
        inner.set_margin_end(28)
        inner.set_margin_bottom(24)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(inner)
        self.append(sw)

        # ---- carousel ----
        self.carousel = Adw.Carousel(allow_scroll_wheel=False, spacing=12)
        self.carousel.set_size_request(-1, _HERO_H)
        hero = Gtk.Overlay()
        hero.set_child(self.carousel)
        dots = Adw.CarouselIndicatorDots(carousel=self.carousel,
                                         halign=Gtk.Align.CENTER,
                                         valign=Gtk.Align.END, margin_bottom=8)
        hero.add_overlay(dots)
        inner.append(hero)
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

    def _section(self, parent, title, link=None, target=None):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(10)
        head = Gtk.Box()
        lbl = Gtk.Label(label=title, xalign=0, hexpand=True)
        lbl.add_css_class("ashy-section-title")
        head.append(lbl)
        if link:
            btn = Gtk.Button(label=link)
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

    @staticmethod
    def _fill(box, row, items, make) -> None:
        clear_box(row)
        for item in items:
            row.append(make(item))
        box.set_visible(bool(items))

    # ---- hero ----------------------------------------------------------

    def _build_hero(self) -> None:
        while self.carousel.get_n_pages():
            self.carousel.remove(self.carousel.get_nth_page(0))
        # The newest titles with a picture, movies and series in turns.
        featured = home.featured(
            self.storage.channels(MediaType.MOVIE, order="added", limit=40),
            self.storage.channels(MediaType.SERIES, order="added", limit=40))
        if not featured:
            self.carousel.append(self._welcome_slide())
            return
        for ch in featured:
            self.carousel.append(self._slide(ch))
        self._on_slide(0)

    def _welcome_slide(self) -> Gtk.Widget:
        slide = Gtk.Overlay(hexpand=True)
        bg = Gtk.Box()
        bg.add_css_class("ashy-hero-fallback")
        slide.set_child(bg)
        slide.add_overlay(self._hero_text(
            _("Welcome to AshyTV"),
            _("Your channels, movies and series in one place"),
            [(_("View channels"), lambda: self.emit("navigate", "channels"))]))
        return slide

    def _slide(self, ch: Channel) -> Gtk.Widget:
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
        buttons = [(_("Details"), lambda: self.emit("play", ch))]
        if ch.media_type is MediaType.MOVIE:
            buttons.insert(0, (_("Watch now"), lambda: self.emit("watch", ch)))
        slide.add_overlay(self._hero_text(ch.display_name, meta, buttons, ch.plot))
        return slide

    def _hero_text(self, title, sub, buttons, plot="") -> Gtk.Widget:
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
        row = Gtk.Box(spacing=10, halign=Gtk.Align.START, margin_top=6)
        for i, (label, action) in enumerate(buttons):
            btn = Gtk.Button(label=label)
            btn.add_css_class("pill-light" if i == 0 else "pill-ghost")
            btn.connect("clicked", lambda _b, a=action: a())
            row.append(btn)
        box.append(row)
        return box

    def focus_content(self) -> bool:
        """Entering Home lands on the slide on screen (its Watch button)."""
        n = self.carousel.get_n_pages()
        if n:
            page = self.carousel.get_nth_page(min(n - 1, round(self.carousel.get_position())))
            if page.child_focus(Gtk.DirectionType.TAB_FORWARD):
                return True
        return False

    def _on_slide(self, index: int) -> None:
        """Only the slide on screen takes the focus (Tab and arrows must not
        land on a hidden one); a focused button follows the slide."""
        root = self.get_root()
        focus = root.get_focus() if root is not None else None
        had_focus = focus is not None and focus.is_ancestor(self.carousel)
        for i in range(self.carousel.get_n_pages()):
            self.carousel.get_nth_page(i).set_can_focus(i == index)
        if had_focus and index < self.carousel.get_n_pages():
            self.carousel.get_nth_page(index).child_focus(Gtk.DirectionType.TAB_FORWARD)

    def _advance(self) -> bool:
        n = self.carousel.get_n_pages()
        if n > 1 and self.get_mapped():
            nxt = (round(self.carousel.get_position()) + 1) % n
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
