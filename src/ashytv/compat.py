"""Fallbacks for older GTK / libadwaita.

The AppImage is built on Ubuntu 24.04 (GTK 4.14, libadwaita 1.5) so it runs
on hosts with an older glibc; the distro package gets whatever the system
ships. Newer widgets are used when present and replaced by their closest
older equivalent otherwise.
"""

from __future__ import annotations

import re

from gi.repository import Adw, Gtk

# GTK 4.20: CssProvider evaluates `@media (prefers-color-scheme)`. Older GTK
# also lacks CSS variables (< 4.16), so the stylesheet is flattened instead.
MODERN_CSS = hasattr(Gtk, "InterfaceColorScheme")


def spinner(**props) -> Gtk.Widget:
    """Adw.Spinner (libadwaita 1.6) or a spinning Gtk.Spinner."""
    if hasattr(Adw, "Spinner"):
        return Adw.Spinner(**props)
    return Gtk.Spinner(spinning=True, **props)


def button_row(title: str) -> Adw.PreferencesRow:
    """Adw.ButtonRow (libadwaita 1.6) or an activatable Adw.ActionRow.
    Both emit "activated"."""
    if hasattr(Adw, "ButtonRow"):
        return Adw.ButtonRow(title=title)
    return Adw.ActionRow(title=title, activatable=True)


# ---- stylesheet ----------------------------------------------------------

_ROOT_RE = re.compile(r":root\s*\{([^{}]*)\}")
_MEDIA_RE = re.compile(
    r"@media\s*\(\s*prefers-color-scheme\s*:\s*(light|dark)\s*\)\s*\{\s*"
    r":root\s*\{([^{}]*)\}\s*\}"
)
_DECL_RE = re.compile(r"(--[\w-]+)\s*:\s*([^;]+);")
_VAR_RE = re.compile(r"var\(\s*(--[\w-]+)\s*\)")
_MIX_RE = re.compile(
    r"color-mix\(\s*in\s+srgb\s*,\s*([^,]+?)\s+calc\(\s*([\d.]+)\s*\*\s*100%\s*\)"
    r"\s*,\s*transparent\s*\)"
)
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


def flatten_css(text: str, dark: bool, overrides: dict[str, str]) -> str:
    """Resolve the stylesheet's custom properties into literal values for
    GTK < 4.20: picks the light/dark `:root` block, applies ``overrides``
    (accent and window opacity from the settings), substitutes every
    ``var()`` and turns ``color-mix(... calc(a * 100%), transparent)`` into
    ``alpha()``."""
    text = _COMMENT_RE.sub("", text)
    variables: dict[str, str] = {}
    scheme_vars: dict[str, dict[str, str]] = {"light": {}, "dark": {}}

    def take_media(match: re.Match) -> str:
        scheme_vars[match.group(1)].update(_DECL_RE.findall(match.group(2)))
        return ""

    def take_root(match: re.Match) -> str:
        variables.update(_DECL_RE.findall(match.group(1)))
        return ""

    text = _MEDIA_RE.sub(take_media, text)
    text = _ROOT_RE.sub(take_root, text)
    variables.update(scheme_vars["dark" if dark else "light"])
    variables.update(overrides)

    def resolve(value: str, depth: int = 0) -> str:
        if depth > 8:
            return value
        return _VAR_RE.sub(
            lambda m: resolve(variables.get(m.group(1), m.group(0)), depth + 1), value
        )

    text = resolve(text)
    text = _MIX_RE.sub(lambda m: f"alpha({m.group(1).strip()}, {m.group(2)})", text)

    # libadwaita's accent: named colors up to 1.5, CSS variables from 1.6.
    accent = {
        name: resolve(variables[f"--{name}"])
        for name in ("accent-bg-color", "accent-color", "accent-fg-color")
        if f"--{name}" in variables
    }
    header = [f"@define-color {n.replace('-', '_')} {v};" for n, v in accent.items()]
    if accent and Gtk.check_version(4, 16, 0) is None:  # CSS variables
        header.append(":root { " + " ".join(f"--{n}: {v};" for n, v in accent.items()) + " }")
    return "\n".join(header) + "\n" + text
