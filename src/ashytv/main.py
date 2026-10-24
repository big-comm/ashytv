"""AshyTV entry point."""

from __future__ import annotations

import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

if not __package__:
    # Executed as a script (`python main.py`): make the package importable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ashytv.application import AshyTvApplication  # noqa: E402


def main() -> int:
    app = AshyTvApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
