#!/usr/bin/env bash
set -euo pipefail

APP_ID="io.github.ddm.DoDownloadManager"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1
TR=0
case "${LANGUAGE:-${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}}" in tr*) TR=1 ;; esac
T() { if [ "$TR" -eq 1 ]; then printf '%s' "$1"; else printf '%s' "$2"; fi; }

"$HOME/.local/bin/ddm" --quit >/dev/null 2>&1 || true

rm -f "$HOME/.local/bin/ddm"
rm -f "$DATA/applications/$APP_ID.desktop"
rm -f "$DATA/icons/hicolor/scalable/apps/$APP_ID.svg" "$DATA/icons/hicolor/128x128/apps/$APP_ID.png" "$DATA/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg"
rm -f "$CONF/autostart/$APP_ID.desktop"
rm -rf "$DATA/ddm/app" "$DATA/ddm/extension"

if [ "$PURGE" -eq 1 ]; then
  rm -rf "$DATA/ddm" "$CONF/ddm"
  echo "$(T "DDM ve tüm ayarları/geçmişi kaldırıldı. İndirilen dosyalara dokunulmadı." "DDM and all its settings/history were removed. Downloaded files were not touched.")"
else
  echo "$(T "DDM kaldırıldı. Ayarlar ve indirme geçmişi duruyor (silmek için: ./uninstall.sh --purge)." "DDM was removed. Settings and download history were kept (to delete them: ./uninstall.sh --purge).")"
fi
