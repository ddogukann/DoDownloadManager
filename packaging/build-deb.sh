#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG="dodownloadmanager"
APP_ID="io.github.ddm.DoDownloadManager"
MAINTAINER="${DDM_MAINTAINER:-Doğukan <ddogukann@users.noreply.github.com>}"
VERSION="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$ROOT/ddm/__init__.py")"
DATE="$(date -u +%Y-%m-%d)"
OUT="$ROOT/dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

[ -n "$VERSION" ] || { echo "VERSION not found in ddm/__init__.py" >&2; exit 1; }

mkdir -p "$STAGE/usr/lib/ddm" "$STAGE/usr/bin" "$STAGE/DEBIAN" "$OUT"
cp -r "$ROOT/ddm" "$STAGE/usr/lib/ddm/"
find "$STAGE/usr/lib/ddm" -name '__pycache__' -type d -prune -exec rm -rf {} +

cat > "$STAGE/usr/bin/ddm" <<'EOF'
#!/bin/sh
export PYTHONPATH="/usr/lib/ddm${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m ddm "$@"
EOF
chmod 755 "$STAGE/usr/bin/ddm"

install -Dm644 "$ROOT/data/$APP_ID.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"
install -Dm644 "$ROOT/data/$APP_ID-symbolic.svg" "$STAGE/usr/share/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg"
install -Dm644 "$ROOT/extension/icons/icon-128.png" "$STAGE/usr/share/icons/hicolor/128x128/apps/$APP_ID.png"
mkdir -p "$STAGE/usr/share/applications" "$STAGE/usr/share/metainfo"
sed "s|@EXEC@|ddm|g" "$ROOT/data/$APP_ID.desktop.in" > "$STAGE/usr/share/applications/$APP_ID.desktop"
sed -e "s|@VERSION@|$VERSION|" -e "s|@DATE@|$DATE|" "$ROOT/data/$APP_ID.metainfo.xml" \
    > "$STAGE/usr/share/metainfo/$APP_ID.metainfo.xml"
chmod 644 "$STAGE/usr/share/applications/$APP_ID.desktop" "$STAGE/usr/share/metainfo/$APP_ID.metainfo.xml"

install -Dm644 "$ROOT/data/ddm.1" "$STAGE/usr/share/man/man1/ddm.1"
gzip -9n "$STAGE/usr/share/man/man1/ddm.1"

DOC="$STAGE/usr/share/doc/$PKG"
mkdir -p "$DOC"
cat > "$DOC/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: DoDownloadManager
Source: https://github.com/ddogukann/DoDownloadManager

Files: *
Copyright: 2026 Doğukan
License: GPL-3.0-or-later
 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.
 .
 On Debian systems, the full text of the GNU General Public License
 version 3 can be found in the file /usr/share/common-licenses/GPL-3.
EOF
printf '%s (%s) stable; urgency=medium\n\n  * Release %s. See https://github.com/ddogukann/DoDownloadManager/releases\n\n -- %s  %s\n' \
    "$PKG" "$VERSION" "$VERSION" "$MAINTAINER" "$(date -u -R)" | gzip -9n > "$DOC/changelog.gz"
chmod 644 "$DOC/copyright" "$DOC/changelog.gz"

cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    python3 -m compileall -q /usr/lib/ddm >/dev/null 2>&1 || true
fi
EOF
cat > "$STAGE/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
find /usr/lib/ddm -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
EOF
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm"

SIZE="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Architecture: all
Maintainer: $MAINTAINER
Installed-Size: $SIZE
Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, python3-cairo, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5), python3-requests
Recommends: ffmpeg, 7zip | p7zip-full
Section: net
Priority: optional
Homepage: https://github.com/ddogukann/DoDownloadManager
Description: Multi-connection download manager with video downloads
 DoDownloadManager (DDM) splits downloads into parallel connections,
 resumes them after restarts and downloads videos with yt-dlp. It can
 bond several internet connections, refresh expired links, check files
 with VirusTotal and verify checksums. A Firefox extension hands browser
 downloads over to the app.
EOF

DEB="$OUT/${PKG}_${VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$DEB" >/dev/null
echo "$DEB"
