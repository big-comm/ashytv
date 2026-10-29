"""Home page — featured carousel, continue watching and shortcuts."""

from __future__ import annotations

import time

from gi.repository import Adw, GLib, GObject, Gtk

from ...i18n import _
from ...models import Channel, MediaType
from ...services.storage import collapse_variants
from ._common import clear_box, set_logo, set_picture

_HERO_H = 300
_SLIDE_SECS = 7


class HomePage(Gtk.Box):
    __gtype_name__ = "AshyHomePage"

    __gsignals__ = {
        "play": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
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
        GLib.timeout_add_seconds(_SLIDE_SECS, self._advance)

        # ---- sections ----
        self.continue_box, self.continue_row = self._section(inner, _("Continue Watching"))
        self.live_box, self.live_row = self._section(inner, _("Live Channels"),
                                                     _("See all"), "channels")
        self.movies_box, self.movies_row = self._section(inner, _("Recently Added Movies"),
                                                         _("See all"), "movies")
        self.series_box, self.series_row = self._section(inner, _("Trending Series"),
                                                         _("See all"), "series")

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
        self._fill(self.movies_box, self.movies_row,
                   self.storage.channels(MediaType.MOVIE, order="added", limit=16),
                   self._poster_card)
        series = [c for c in self.storage.channels(MediaType.SERIES, order="added",
                                                   limit=200) if c.rating >= 7][:16]
        self._fill(self.series_box, self.series_row, series, self._poster_card)

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
        featured = [c for c in self.storage.channels(MediaType.SERIES, order="added",
                                                     limit=80)
                    if c.backdrop and c.rating >= 6][:3]
        featured += [c for c in self.storage.channels(MediaType.MOVIE, order="added",
                                                      limit=80)
                     if c.logo and c.rating >= 6][:3]
        if not featured:
            self.carousel.append(self._welcome_slide())
            return
        for ch in featured:
            self.carousel.append(self._slide(ch))

    def _welcome_slide(self) -> Gtk.Widget:
        slide = Gtk.Overlay(hexpand=True)
        bg = Gtk.Box()
        bg.add_css_class("ashy-hero-fallback")
        slide.set_child(bg)
        slide.add_overlay(self._hero_text(
            _("Welcome to AshyTV"),
            _("Your channels, movies and series in one place"),
            _("View channels"), lambda: self.emit("navigate", "channels")))
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
        slide.add_overlay(self._hero_text(ch.display_name, meta, _("Watch now"),
                                          lambda: self.emit("play", ch),
                                          ch.plot))
        return slide

    def _hero_text(self, title, sub, button, action, plot="") -> Gtk.Widget:
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
        btn = Gtk.Button(label=button, halign=Gtk.Align.START, margin_top=6)
        btn.add_css_class("pill-light")
        btn.connect("clicked", lambda *_: action())
        box.append(btn)
        return box

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
        card = Gtk.Button()
        card.add_css_class("flat")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        frame = Gtk.Box()
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        frame.add_css_class("ashy-poster-img")
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        pic.set_size_request(130, 195)
        set_picture(pic, ch.logo, 195)
        frame.append(pic)
        box.append(frame)
        t = Gtk.Label(label=ch.display_name, xalign=0, ellipsize=3, max_width_chars=14)
        t.add_css_class("ashy-poster-title")
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
