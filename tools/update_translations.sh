#!/usr/bin/env bash
# Translation maintenance for AshyTV (GNU gettext).
#
#   tools/update_translations.sh           extract strings into locale/ashytv.pot
#                                          and merge them into every locale/*.po
#   tools/update_translations.sh compile   build usr/share/locale/*/ashytv.mo and
#                                          the translated .desktop / metainfo files
set -euo pipefail
cd "$(dirname "$0")/.."

DOMAIN=ashytv
POT=locale/$DOMAIN.pot
DESKTOP_IN=data/org.communitybig.ashytv.desktop.in
METAINFO_IN=data/org.communitybig.ashytv.metainfo.xml.in

extract() {
    local tmp_py tmp_data
    tmp_py=$(mktemp) tmp_data=$(mktemp)
    find src/ashytv -name '*.py' | sort > "$tmp_py"
    xgettext --language=Python --from-code=UTF-8 --keyword=_ --keyword=ngettext:1,2 \
        --add-comments=Translators: --sort-by-file --package-name=ashytv --package-version=0.1.0 \
        --msgid-bugs-address=https://github.com/big-comm/ashytv/issues \
        --files-from="$tmp_py" -o "$POT"
    xgettext --join-existing --from-code=UTF-8 --add-comments=Translators: \
        -o "$POT" "$DESKTOP_IN" "$METAINFO_IN"
    rm -f "$tmp_py" "$tmp_data"
    echo "Updated $POT ($(grep -c '^msgid ' "$POT") entries)"
}

merge() {
    for lang in $(cat locale/LINGUAS); do
        po=locale/$lang.po
        if [[ -f $po ]]; then
            msgmerge --quiet --update --backup=none --no-fuzzy-matching "$po" "$POT"
        else
            msginit --no-translator --no-wrap --locale="$lang" -i "$POT" -o "$po"
        fi
    done
}

compile() {
    for lang in $(cat locale/LINGUAS); do
        out=usr/share/locale/$lang/LC_MESSAGES
        mkdir -p "$out"
        msgfmt --check -o "$out/$DOMAIN.mo" "locale/$lang.po"
    done
    mkdir -p usr/share/applications usr/share/metainfo
    msgfmt --desktop --template="$DESKTOP_IN" -d locale \
        -o usr/share/applications/org.communitybig.ashytv.desktop
    msgfmt --xml --template="$METAINFO_IN" -d locale \
        -o usr/share/metainfo/org.communitybig.ashytv.metainfo.xml
    echo "Compiled $(wc -l < locale/LINGUAS) languages"
}

case "${1:-update}" in
    update) extract; merge ;;
    extract) extract ;;
    compile) compile ;;
    *) echo "usage: $0 [update|extract|compile]" >&2; exit 2 ;;
esac
