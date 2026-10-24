"""AshyTV entry point."""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from .application import AshyTvApplication  # noqa: E402


def main() -> int:
    app = AshyTvApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
