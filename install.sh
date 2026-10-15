#!/usr/bin/env bash
set -euo pipefail

APP_ID="io.github.ddm.DoDownloadManager"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
APPDIR="$DATA/ddm/app"
EXTDIR="$DATA/ddm/extension"
BINDIR="$DATA/ddm/bin"
LAUNCHER="$HOME/.local/bin/ddm"
SKIP_YTDLP=0
SKIP_APT=0
TR=0
case "${LANGUAGE:-${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}}" in tr*) TR=1 ;; esac
T() { if [ "$TR" -eq 1 ]; then printf '%s' "$1"; else printf '%s' "$2"; fi; }

for arg in "$@"; do
  case "$arg" in
    --no-ytdlp) SKIP_YTDLP=1 ;;
    --no-apt) SKIP_APT=1 ;;
    -h|--help)
      echo "$(T "Kullanım" "Usage"): ./install.sh [--no-apt] [--no-ytdlp]"
      exit 0 ;;
  esac
done

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

PKGS=(python3 python3-gi python3-gi-cairo python3-cairo gir1.2-gtk-4.0 gir1.2-adw-1 python3-requests ffmpeg)
if [ "$SKIP_APT" -eq 0 ]; then
  MISSING=()
  for p in "${PKGS[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p")
  done
  if [ "${#MISSING[@]}" -gt 0 ]; then
    say "$(T "Eksik paketler kuruluyor" "Installing missing packages"): ${MISSING[*]}"
    sudo apt-get update -q
    sudo apt-get install -y "${MISSING[@]}"
  fi
fi

PY=""
for cand in /usr/bin/python3 /usr/bin/python3.13 /usr/bin/python3.12 /usr/bin/python3.11; do
  if [ -x "$cand" ] && "$cand" -c "import gi" >/dev/null 2>&1; then
    PY="$cand"
    break
  fi
done
[ -n "$PY" ] || { echo "$(T "python3-gi bulunamadı" "python3-gi not found"). sudo apt install python3-gi"; exit 1; }

"$PY" - <<'PY'
import sys
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk
if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 12) or Adw.get_minor_version() < 5:
    sys.exit("GTK 4.12+ / libadwaita 1.5+ (Ubuntu 24.04+)")
PY

say "$(T "Uygulama kopyalanıyor" "Copying the app") → $APPDIR"
rm -rf "$APPDIR"
mkdir -p "$APPDIR" "$HOME/.local/bin" "$BINDIR"
cp -r "$SRC/ddm" "$APPDIR/"
find "$APPDIR" -name '__pycache__' -type d -prune -exec rm -rf {} +

cat > "$LAUNCHER" <<EOF
#!/bin/sh
export PYTHONPATH="$APPDIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec $PY -m ddm "\$@"
EOF
chmod +x "$LAUNCHER"

say "$(T "Masaüstü girdisi ve simge kuruluyor" "Installing desktop entry and icons")"
install -Dm644 "$SRC/data/$APP_ID.svg" "$DATA/icons/hicolor/scalable/apps/$APP_ID.svg"
install -Dm644 "$SRC/data/$APP_ID-symbolic.svg" "$DATA/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg"
install -Dm644 "$SRC/extension/icons/icon-128.png" "$DATA/icons/hicolor/128x128/apps/$APP_ID.png"
mkdir -p "$DATA/applications"
sed "s|@EXEC@|$LAUNCHER|g" "$SRC/data/$APP_ID.desktop.in" > "$DATA/applications/$APP_ID.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true

if [ "$SKIP_YTDLP" -eq 0 ]; then
  if [ -x "$BINDIR/yt-dlp" ]; then
    say "$(T "yt-dlp güncelleniyor" "Updating yt-dlp")"
    "$BINDIR/yt-dlp" -U >/dev/null 2>&1 || warn "$(T "yt-dlp güncellenemedi (uygulama içinden tekrar deneyebilirsiniz)" "Could not update yt-dlp (you can retry from the app)")"
  else
    say "$(T "yt-dlp indiriliyor" "Downloading yt-dlp")"
    if curl -fsSL -o "$BINDIR/yt-dlp.tmp" https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp; then
      chmod +x "$BINDIR/yt-dlp.tmp"
      mv "$BINDIR/yt-dlp.tmp" "$BINDIR/yt-dlp"
    else
      rm -f "$BINDIR/yt-dlp.tmp"
      warn "$(T "yt-dlp indirilemedi. Ayarlar ▸ Video ▸ İndir düğmesiyle sonra kurabilirsiniz." "Could not download yt-dlp. You can install it later in Settings ▸ Video.")"
    fi
  fi
  if PYTHONPATH="$APPDIR" "$PY" -c "import sys; from ddm.video import js_runtime; sys.exit(0 if js_runtime() else 1)" 2>/dev/null; then
    :
  else
    say "$(T "deno indiriliyor (YouTube için gerekli)" "Downloading deno (required for YouTube)")"
    PYTHONPATH="$APPDIR" "$PY" -c "from ddm.video import update_deno; update_deno()" 2>/dev/null \
      || warn "$(T "deno indirilemedi. Ayarlar ▸ Video bölümünden sonra kurabilirsiniz." "Could not download deno. You can install it later in Settings ▸ Video.")"
  fi
fi

say "$(T "Firefox eklentisi paketleniyor" "Packaging the Firefox extension") → $EXTDIR"
rm -rf "$EXTDIR"
mkdir -p "$EXTDIR"
cp -r "$SRC/extension" "$EXTDIR/unpacked"
"$PY" - "$SRC/extension" "$EXTDIR/ddm-firefox.xpi" <<'PY'
import os, sys, zipfile
src, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(src):
        for f in sorted(files):
            if f.startswith("_") or f.startswith("."):
                continue
            full = os.path.join(root, f)
            z.write(full, os.path.relpath(full, src))
PY

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) warn "$(T "~/.local/bin PATH içinde değil; yeni bir oturum açın ya da PATH'e ekleyin." "~/.local/bin is not in PATH; log in again or add it to PATH.")" ;;
esac

if [ "$TR" -eq 1 ]; then
cat <<EOF

$(printf '\033[1;32m')Kurulum tamamlandı.$(printf '\033[0m')

  Uygulama:   Etkinlikler ▸ "DoDownloadManager"   ya da   ddm
  Eklenti:    $EXTDIR/ddm-firefox.xpi
              (kurulum seçenekleri için README.tr.md ▸ "Firefox eklentisi")

EOF
else
cat <<EOF

$(printf '\033[1;32m')Installation complete.$(printf '\033[0m')

  App:        Activities ▸ "DoDownloadManager"   or   ddm
  Extension:  $EXTDIR/ddm-firefox.xpi
              (see README.md ▸ "Firefox extension" for install options)

EOF
fi
