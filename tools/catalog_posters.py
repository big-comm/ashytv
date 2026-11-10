#!/usr/bin/env python3
"""Puts a proper poster on each film in the free catalogue.

Why this exists: the apps draw a card from the playlist's own ``tvg-logo``
and never ask the metadata service for a picture. For a provider's playlist
that is right — they ship their own art. For ours it is not: the logo we had
was the still frame the Internet Archive generates, which makes a catalogue
of good films look like an abandoned file server.

Rewriting the catalogue is the only part of this that needs no change in any
app, on any platform, and no release: it is data.

    python3 tools/catalog_posters.py [--panel https://painel.ashytv.com] [--dry-run]

Each film is looked up by title and year through the panel (never TMDB
directly — the panel is what holds the key and proxies the pictures). A film
TMDB does not know keeps the logo it has; nothing is ever blanked.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
CATALOGO = RAIZ / "src" / "ashytv" / "data" / "free_catalog.m3u"
UA = {"User-Agent": "AshyTV catalog tool (+https://github.com/big-comm/ashytv)"}

# Live channels keep their own logo: a broadcaster's mark is the right picture
# for a channel, and TMDB has nothing to say about it.
PULAR_GRUPOS = {"Open TV"}


def procura(painel: str, titulo: str, ano: str | None, tipo: str) -> str | None:
    """The poster the panel has for this title, or None."""
    q = {"q": titulo, "type": tipo}
    if ano:
        q["year"] = ano
    url = f"{painel}/api/v1/meta/title?" + urllib.parse.urlencode(q)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
            return json.load(r).get("poster")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def limpa(titulo: str) -> tuple[str, str | None]:
    """"Suddenly (1954)" -> ("Suddenly", "1954")."""
    m = re.search(r"^(.*?)\s*\((\d{4})\)\s*$", titulo)
    if m:
        return m.group(1).strip(), m.group(2)
    return titulo.strip(), None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="https://painel.ashytv.com")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    linhas = CATALOGO.read_text().splitlines()
    trocados = achados = pulados = 0

    for i, linha in enumerate(linhas):
        if not linha.startswith("#EXTINF"):
            continue
        grupo = (re.search(r'group-title="([^"]*)"', linha) or [None, ""])[1]
        if grupo in PULAR_GRUPOS:
            continue
        titulo_bruto = linha.split(",", 1)[1] if "," in linha else ""
        titulo, ano = limpa(titulo_bruto)
        # Series entries name an episode; the show is what TMDB knows.
        tipo = "tv" if grupo.startswith("Series") else "movie"
        if tipo == "tv":
            titulo = re.split(r"\s+S\d{2}E\d{2}", titulo)[0].strip()

        try:
            poster = procura(args.panel, titulo, ano, tipo)
        except Exception as e:  # report and carry on
            print(f"  ?  {titulo[:42]:44s} {type(e).__name__}")
            continue
        if not poster:
            pulados += 1
            print(f"  -  {titulo[:42]:44s} TMDB não tem")
            continue
        achados += 1
        nova = re.sub(r'tvg-logo="[^"]*"', f'tvg-logo="{poster}"', linha)
        if 'tvg-logo="' not in linha:
            nova = linha.replace("#EXTINF:-1 ", f'#EXTINF:-1 tvg-logo="{poster}" ', 1)
        if nova != linha:
            linhas[i] = nova
            trocados += 1
            print(f"  ok {titulo[:42]:44s} {poster.rsplit('/', 1)[-1]}")

    print(f"\nencontrados no TMDB: {achados} | trocados: {trocados} | sem pôster: {pulados}")
    if args.dry_run:
        print("ensaio: nada foi escrito")
        return 0
    CATALOGO.write_text("\n".join(linhas) + "\n")
    print(f"gravado: {CATALOGO}")
    print("lembre de copiar para shared/free_catalog.m3u no repositório AshyTV")
    return 0


if __name__ == "__main__":
    sys.exit(main())
