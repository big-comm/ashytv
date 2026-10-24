"""Test configuration: run with untranslated (English) strings."""

import os

# Must be set before ashytv.i18n is used: gettext reads it on every lookup.
os.environ["LANGUAGE"] = "C"
os.environ["LC_MESSAGES"] = "C"
