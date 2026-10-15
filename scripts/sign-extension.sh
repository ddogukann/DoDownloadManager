#!/usr/bin/env bash
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$SRC/dist"

TR=0
case "${LANGUAGE:-${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}}" in tr*) TR=1 ;; esac
T() { if [ "$TR" -eq 1 ]; then printf '%s' "$1"; else printf '%s' "$2"; fi; }
if [ -z "${AMO_JWT_ISSUER:-}" ] || [ -z "${AMO_JWT_SECRET:-}" ]; then
  if [ "$TR" -eq 1 ]; then
    cat <<'EOF'
AMO API anahtarı gerekli:
  1. https://addons.mozilla.org/developers/addon/api/key/ adresinden anahtar oluşturun
  2. export AMO_JWT_ISSUER="user:12345:67"
     export AMO_JWT_SECRET="..."
  3. ./scripts/sign-extension.sh
Eklenti "unlisted" olarak imzalanır; mağazada yayınlanmaz, yalnızca sizin kurmanız için imzalı .xpi üretilir.
EOF
  else
    cat <<'EOF'
An AMO API key is required:
  1. Create one at https://addons.mozilla.org/developers/addon/api/key/
  2. export AMO_JWT_ISSUER="user:12345:67"
     export AMO_JWT_SECRET="..."
  3. ./scripts/sign-extension.sh
The extension is signed as "unlisted": it is not published in the store, you just get a signed .xpi to install.
EOF
  fi
  exit 1
fi

command -v npx >/dev/null || { echo "$(T "Node.js gerekli" "Node.js is required"): sudo apt install nodejs npm"; exit 1; }
mkdir -p "$OUT"
npx --yes web-ext@8 sign \
  --source-dir "$SRC/extension" \
  --artifacts-dir "$OUT" \
  --channel unlisted \
  --api-key "$AMO_JWT_ISSUER" \
  --api-secret "$AMO_JWT_SECRET"
echo "$(T "İmzalı eklenti" "Signed extension"): $OUT"
