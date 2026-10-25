"""Spatial arrow-key navigation, like a TV remote.

An arrow key moves the focus to the nearest focusable widget in that
direction. Lists and grids count as one block: inside them the arrows move
between items as usual, and at their edge the focus leaves to the neighbour
(GTK list views keep the key otherwise, and the focus gets stuck).
"""

from __future__ import annotations

from dataclasses import dataclass

from gi.repository import Adw, Gtk

_COLLECTIONS = (Gtk.ListView, Gtk.GridView, Gtk.ListBox, Gtk.FlowBox)
# Widgets that use Left/Right themselves (cursor, value).
_HORIZONTAL_KEEPERS = (Gtk.Editable, Gtk.Scale)


@dataclass(slots=True)
class _Rect:
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2

    def intersect(self, other: _Rect) -> _Rect | None:
        r = _Rect(max(self.x1, other.x1), max(self.y1, other.y1),
                  min(self.x2, other.x2), min(self.y2, other.y2))
        return r if r.x2 > r.x1 and r.y2 > r.y1 else None


def _bounds(widget: Gtk.Widget, root: Gtk.Widget) -> _Rect | None:
    ok, b = widget.compute_bounds(root)
    if not ok or b.get_width() <= 0 or b.get_height() <= 0:
        return None
    return _Rect(b.get_x(), b.get_y(), b.get_x() + b.get_width(),
                 b.get_y() + b.get_height())


def _ancestor(widget: Gtk.Widget | None, types) -> Gtk.Widget | None:
    while widget is not None:
        if isinstance(widget, types):
            return widget
        widget = widget.get_parent()
    return None


def _scope(focus: Gtk.Widget, window: Gtk.Widget) -> Gtk.Widget:
    """Navigation stays inside the dialog or popover that has the focus."""
    return _ancestor(focus, (Adw.Dialog, Gtk.Popover)) or window


def _visible_rect(widget: Gtk.Widget, root: Gtk.Widget,
                  focus: Gtk.Widget) -> _Rect | None:
    """Bounds clipped by the scrolled windows the focus is not inside.

    Items of the scroller that holds the focus stay reachable even when
    scrolled out of view (moving to them scrolls); hidden parts of other
    scrollers do not.
    """
    rect = _bounds(widget, root)
    parent = widget.get_parent()
    while rect is not None and parent is not None and parent is not root:
        if isinstance(parent, Gtk.ScrolledWindow) and not focus.is_ancestor(parent):
            area = _bounds(parent, root)
            rect = rect.intersect(area) if area else None
        parent = parent.get_parent()
    return rect


def _usable(widget: Gtk.Widget) -> bool:
    return widget.get_visible() and widget.get_mapped() and widget.is_sensitive()


def _candidates(root: Gtk.Widget, skip: Gtk.Widget | None):
    """Focusable leaves and collections (as one target) under root."""
    stack = [root.get_first_child()]
    while stack:
        widget = stack.pop()
        while widget is not None:
            if widget is not skip and _usable(widget) and not isinstance(
                    widget, (Gtk.Scrollbar, Gtk.Popover, Adw.Dialog)):
                if isinstance(widget, _COLLECTIONS):
                    if _has_items(widget):
                        yield widget
                elif widget.get_focusable() and widget.get_can_focus() and not \
                        isinstance(widget, (Gtk.ScrolledWindow, Gtk.Viewport)):
                    yield widget  # its children (labels, icons…) are part of it
                else:
                    stack.append(widget.get_first_child())
            widget = widget.get_next_sibling()


def _has_items(collection: Gtk.Widget) -> bool:
    if isinstance(collection, (Gtk.ListView, Gtk.GridView)):
        model = collection.get_model()
        return model is not None and model.get_n_items() > 0
    return collection.get_first_child() is not None


def _score(src: _Rect, dst: _Rect, direction: Gtk.DirectionType) -> float | None:
    """Distance to a target in that direction (None = not in that direction)."""
    D = Gtk.DirectionType
    if direction in (D.LEFT, D.RIGHT):
        ahead = dst.cx > src.cx + 1 if direction == D.RIGHT else dst.cx < src.cx - 1
        gap = (dst.x1 - src.x2) if direction == D.RIGHT else (src.x1 - dst.x2)
        perp = max(0.0, dst.y1 - src.y2, src.y1 - dst.y2)
        center = abs(dst.cy - src.cy)
        if perp > 0:
            return None  # sideways moves stay on the same line
    else:
        ahead = dst.cy > src.cy + 1 if direction == D.DOWN else dst.cy < src.cy - 1
        gap = (dst.y1 - src.y2) if direction == D.DOWN else (src.y1 - dst.y2)
        perp = max(0.0, dst.x1 - src.x2, src.x1 - dst.x2)
        center = abs(dst.cx - src.cx)
    if not ahead:
        return None
    # Targets in line with the focus always win over ones off to the side.
    return (10000.0 if perp > 0 else 0.0) + max(0.0, gap) + perp * 3 + center * 0.05


def _items(collection: Gtk.Widget):
    child = collection.get_first_child()
    while child is not None:
        if _usable(child) and child.get_focusable() and not isinstance(
                child, Gtk.Scrollbar):
            yield child
        child = child.get_next_sibling()


def _item_of(focus: Gtk.Widget, collection: Gtk.Widget) -> Gtk.Widget | None:
    item = focus
    while item is not None and item.get_parent() is not collection:
        item = item.get_parent()
    return item


def _stays_inside(focus: Gtk.Widget, collection: Gtk.Widget, root: Gtk.Widget,
                  direction: Gtk.DirectionType) -> bool:
    """Whether the collection has another item in that direction."""
    item = _item_of(focus, collection)
    src = _bounds(item, root) if item is not None else None
    if src is None:
        return False
    D = Gtk.DirectionType
    for other in _items(collection):
        if other is item:
            continue
        dst = _bounds(other, root)
        if dst is None:
            continue
        if direction in (D.UP, D.DOWN):
            same_column = dst.x1 < src.x2 and dst.x2 > src.x1
            if same_column and (dst.cy > src.cy if direction == D.DOWN else dst.cy < src.cy):
                return True
        else:
            same_row = dst.y1 < src.y2 and dst.y2 > src.y1
            if same_row and (dst.cx > src.cx if direction == D.RIGHT else dst.cx < src.cx):
                return True
    return False


def _focus_collection(collection: Gtk.Widget, root: Gtk.Widget, src: _Rect,
                      direction: Gtk.DirectionType, focus: Gtk.Widget) -> bool:
    """Enter a list/grid on the selected item, or the one nearest the source."""
    area = _visible_rect(collection, root, focus)
    if isinstance(collection, (Gtk.ListView, Gtk.GridView)):
        model = collection.get_model()
        if isinstance(model, Gtk.SingleSelection):
            pos = model.get_selected()
            if pos != Gtk.INVALID_LIST_POSITION:
                # Use it only when it is on screen; otherwise the nearest one.
                for item in _items(collection):
                    r = _bounds(item, root)
                    if r and area and r.intersect(area) and item.get_state_flags() \
                            & Gtk.StateFlags.SELECTED:
                        collection.scroll_to(pos, Gtk.ListScrollFlags.FOCUS, None)
                        return True
    elif isinstance(collection, Gtk.ListBox):
        row = collection.get_selected_row()
        r = _bounds(row, root) if row is not None else None
        if r and area and r.intersect(area):
            return row.grab_focus()
    best, best_score = None, None
    D = Gtk.DirectionType
    for item in _items(collection):
        r = _bounds(item, root)
        if r is None or (area and not r.intersect(area)):
            continue
        # The item closest to where we come from, along the edge we enter.
        along = abs(r.cy - src.cy) if direction in (D.LEFT, D.RIGHT) else abs(r.cx - src.cx)
        depth = {D.DOWN: r.y1, D.UP: -r.y2, D.RIGHT: r.x1, D.LEFT: -r.x2}[direction]
        score = along + depth * 0.01
        if best_score is None or score < best_score:
            best, best_score = item, score
    if best is None:
        return collection.child_focus(Gtk.DirectionType.TAB_FORWARD)
    return best.grab_focus()


def _step_within(focus: Gtk.Widget, container: Gtk.Widget, root: Gtk.Widget,
                 direction: Gtk.DirectionType) -> bool:
    """Move between the focusable widgets inside one list item."""
    src = _bounds(focus, root)
    if src is None:
        return False
    targets = [container] if container.get_focusable() else []
    targets += [w for w in _candidates(container, None) if w is not focus]
    best, best_score = None, None
    for cand in targets:
        if cand is focus or (cand is container and focus is not container
                             and direction == Gtk.DirectionType.RIGHT):
            continue
        rect = _bounds(cand, root)
        if cand is container:
            # Going back left from an inner button returns to the item itself.
            if direction != Gtk.DirectionType.LEFT or focus is container:
                continue
            rect = _Rect(rect.x1, rect.y1, rect.x1 + 1, rect.y2) if rect else None
        score = _score(src, rect, direction) if rect else None
        if score is not None and (best_score is None or score < best_score):
            best, best_score = cand, score
    return best is not None and best.grab_focus()


def keeps_key(focus: Gtk.Widget | None, direction: Gtk.DirectionType) -> bool:
    """The focused widget uses this arrow itself (text cursor, slider…)."""
    if focus is None:
        return False
    if direction in (Gtk.DirectionType.LEFT, Gtk.DirectionType.RIGHT):
        return _ancestor(focus, _HORIZONTAL_KEEPERS) is not None
    return _ancestor(focus, (Gtk.TextView,)) is not None


def move(window: Gtk.Widget, direction: Gtk.DirectionType,
         prefer: dict | None = None, walls: tuple = (),
         root: Gtk.Widget | None = None) -> bool:
    """Move the focus one step in `direction`. Returns True when handled.

    `prefer` maps a container to a callable: when the chosen target is inside
    that container, the callable focuses it instead (e.g. the sidebar's
    current page). `walls` are containers that Up/Down never cross: the
    sidebar is entered and left sideways only. `root` limits the search.
    """
    focus = window.get_focus()
    if focus is None or keeps_key(focus, direction):
        return False
    root = root or _scope(focus, window)
    vertical = direction in (Gtk.DirectionType.UP, Gtk.DirectionType.DOWN)
    inside = next((w for w in walls if focus.is_ancestor(w)), None) if vertical else None
    if inside is not None:
        root = inside
    collection = _ancestor(focus, _COLLECTIONS)
    if collection is not None and not collection.is_ancestor(root) and collection is not root:
        collection = None
    if collection is not None and direction in (Gtk.DirectionType.LEFT,
                                                Gtk.DirectionType.RIGHT):
        # Buttons inside the item (favorite, menu…) come first.
        item = _item_of(focus, collection)
        if item is not None and _step_within(focus, item, root, direction):
            return True
    if collection is not None and _stays_inside(focus, collection, root, direction):
        return False  # let the list move between its own items
    origin = _item_of(focus, collection) if collection is not None else focus
    src = _bounds(origin or focus, root)
    if src is None:
        return False
    best, best_score = None, None
    for cand in _candidates(root, collection or focus):
        if cand.is_ancestor(focus) or focus.is_ancestor(cand) or cand is focus:
            continue
        if vertical and inside is None and any(cand.is_ancestor(w) for w in walls):
            continue
        rect = _visible_rect(cand, root, focus)
        if rect is None:
            continue
        score = _score(src, rect, direction)
        if score is not None and (best_score is None or score < best_score):
            best, best_score = cand, score
    if best is None:
        return True  # nothing there: keep the focus where it is
    for container, action in (prefer or {}).items():
        if (best is container or best.is_ancestor(container)) \
                and not focus.is_ancestor(container):
            action()
            return True
    if isinstance(best, _COLLECTIONS):
        return _focus_collection(best, root, src, direction, focus) or True
    (_selected_sibling(best, focus) or best).grab_focus()
    return True


def _selected_sibling(widget: Gtk.Widget, focus: Gtk.Widget) -> Gtk.Widget | None:
    """Entering a row of chips/tabs lands on the selected one."""
    parent = widget.get_parent()
    if parent is None or focus.is_ancestor(parent):
        return None
    child = parent.get_first_child()
    while child is not None:
        if child is not widget and _usable(child) and child.get_focusable() and (
                child.has_css_class("selected")
                or (isinstance(child, Gtk.ToggleButton) and child.get_active())):
            return child
        child = child.get_next_sibling()
    return None
