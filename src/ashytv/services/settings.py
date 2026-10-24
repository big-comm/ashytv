"""User preferences in JSON (~/.config/ashytv/settings.json).

GSettings would require a compiled/installed schema, which gets in the way of
running straight from source; once Meson packaging exists, we can migrate.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from gi.repository import GLib

from ..i18n import _

DEFAULTS: dict = {
    "theme": "dark",            # dark | light | system
    "accent": "blue",           # ACCENTS key
    "translucent": True,        # translucent panels
    "window_opacity": 0.94,     # window background opacity (translucent)
    "show_logos": True,
    "hwdec": True,
    "auto_quality": True,       # adapt between quality variants
    "group_variants": True,     # show "NEWS FHD/HD/SD" as one channel
    "cache_secs": 20,           # player network buffer (seconds)
    "volume": 100,
    "hide_adult": True,
    "parental_pin": "",
    "auto_refresh": True,
    "refresh_hours": 12,
    "autostart": False,
    "sidebar_compact": False,   # sidebar shows icons only
}


# Accent colors: key -> (label, main color, light color)
ACCENTS: dict[str, tuple[str, str, str]] = {
    "blue": (_("Blue"), "#3584e4", "#62a0ea"),
    "teal": (_("Teal"), "#2190a4", "#3eb4c8"),
    "green": (_("Green"), "#3a944a", "#5cb86b"),
    "yellow": (_("Yellow"), "#c88800", "#e5a50a"),
    "orange": (_("Orange"), "#ed5b00", "#ff7f2a"),
    "red": (_("Red"), "#e62d42", "#f66151"),
    "pink": (_("Pink"), "#d56199", "#e98bbb"),
    "purple": (_("Purple"), "#9141ac", "#b26bc9"),
    "slate": (_("Slate"), "#6f8396", "#93a6b8"),
    "emerald": (_("Emerald"), "#1a9c7f", "#3fc29f"),
}


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or Path(GLib.get_user_config_dir()) / "ashytv" / "settings.json"
        self._data = dict(DEFAULTS)
        self._listeners: dict[str, list[Callable]] = {}
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                self._data.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass

    def __getitem__(self, key: str):
        return self._data[key]

    def get(self, key: str):
        return self._data.get(key, DEFAULTS.get(key))

    def set(self, key: str, value) -> None:
        if self._data.get(key) == value:
            return
        self._data[key] = value
        self._save()
        for cb in self._listeners.get(key, []):
            cb(value)

    def connect(self, key: str, callback: Callable) -> None:
        """callback(value) when the preference changes."""
        self._listeners.setdefault(key, []).append(callback)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass


_AUTOSTART = """[Desktop Entry]
Type=Application
Name=AshyTV
Exec={exec}
Icon=org.communitybig.ashytv
X-GNOME-Autostart-enabled=true
"""


def set_autostart(enabled: bool, command: str = "ashytv") -> None:
    path = Path(GLib.get_user_config_dir()) / "autostart" / "org.communitybig.ashytv.desktop"
    try:
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_AUTOSTART.format(exec=command), encoding="utf-8")
        elif path.exists():
            path.unlink()
    except OSError:
        pass
