"""Translation setup and catalogs."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ashytv import i18n

ROOT = Path(__file__).resolve().parents[1]


def test_locale_dir_prefers_env_then_source_tree(monkeypatch):
    monkeypatch.setenv("ASHYTV_LOCALE_DIR", "/custom/locale")
    assert i18n._locale_dir() == "/custom/locale"
    monkeypatch.delenv("ASHYTV_LOCALE_DIR")
    source_tree = ROOT / "usr" / "share" / "locale"
    expected = str(source_tree) if source_tree.is_dir() else "/usr/share/locale"
    assert i18n._locale_dir() == expected


def test_untranslated_text_falls_back_to_english():
    assert i18n._("Some text that is not in any catalog") == \
        "Some text that is not in any catalog"


@pytest.mark.skipif(shutil.which("msgfmt") is None, reason="gettext tools not installed")
def test_all_catalogs_are_complete_and_valid():
    result = subprocess.run([sys.executable, str(ROOT / "tools" / "check_translations.py")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout[-3000:]
