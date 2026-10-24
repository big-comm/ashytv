#!/usr/bin/env python3
"""Sanity checks for locale/*.po: untranslated entries and broken placeholders.

    python3 tools/check_translations.py [lang ...]

Exits with status 1 when a translation drops/renames a ``{placeholder}`` or
when entries are left untranslated or fuzzy.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

LOCALE = Path(__file__).resolve().parents[1] / "locale"
PLACEHOLDER = re.compile(r"\{[a-z_]*\}")


def _unquote(lines: list[str]) -> str:
    return "".join(
        bytes(line.strip()[1:-1], "utf-8").decode("unicode_escape").encode("latin-1").decode()
        for line in lines)


def parse(path: Path) -> list[dict]:
    entries, cur, field = [], None, None
    for raw in path.read_text(encoding="utf-8").splitlines() + [""]:
        line = raw.strip()
        if not line:
            if cur:
                entries.append(cur)
            cur, field = None, None
            continue
        if cur is None:
            cur = {"flags": "", "msgid": [], "msgid_plural": [], "msgstr": {}}
        if line.startswith("#,"):
            cur["flags"] += line
        elif line.startswith("#"):
            continue
        elif line.startswith("msgid_plural "):
            field = ("msgid_plural", None)
            cur["msgid_plural"].append(line[13:])
        elif line.startswith("msgid "):
            field = ("msgid", None)
            cur["msgid"].append(line[6:])
        elif line.startswith("msgstr"):
            m = re.match(r"msgstr(?:\[(\d+)\])? (.*)", line)
            idx = int(m.group(1) or 0)
            field = ("msgstr", idx)
            cur["msgstr"].setdefault(idx, []).append(m.group(2))
        elif line.startswith('"') and field:
            name, idx = field
            (cur[name] if idx is None else cur[name][idx]).append(line)
    return entries


def check(path: Path) -> list[str]:
    problems = []
    result = subprocess.run(["msgfmt", "--check", "-o", "/dev/null", str(path)],
                            capture_output=True, text=True)
    if result.returncode:
        problems.append(result.stderr.strip())
    for e in parse(path):
        msgid = _unquote(e["msgid"])
        if not msgid:
            continue
        if "fuzzy" in e["flags"]:
            problems.append(f"fuzzy: {msgid!r}")
        expected = set(PLACEHOLDER.findall(msgid + _unquote(e["msgid_plural"])))
        for idx, lines in e["msgstr"].items():
            text = _unquote(lines)
            if not text:
                problems.append(f"untranslated: {msgid!r}")
                break
            found = set(PLACEHOLDER.findall(text))
            if found - expected or (expected and not found & expected and len(e["msgstr"]) == 1):
                problems.append(f"placeholders {sorted(found)} != {sorted(expected)}: {msgid!r}")
    return problems


def main() -> int:
    langs = sys.argv[1:] or (LOCALE / "LINGUAS").read_text().split()
    failed = False
    for lang in langs:
        problems = check(LOCALE / f"{lang}.po")
        status = "ok" if not problems else f"{len(problems)} problem(s)"
        print(f"{lang}: {status}")
        for p in problems[:15]:
            print("   ", p)
        failed |= bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
