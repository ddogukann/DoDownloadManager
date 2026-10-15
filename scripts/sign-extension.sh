#!/usr/bin/env bash
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$SRC/dist"

if [ -z "${AMO_JWT_ISSUER:-}" ] || [ -z "${AMO_JWT_SECRET:-}" ]; then
  cat <<'EOF'
AMO API anahtarı gerekli:
  1. https://addons.mozilla.org/developers/addon/api/key/ adresinden anahtar oluşturun
  2. export AMO_JWT_ISSUER="user:12345:67"
     export AMO_JWT_SECRET="..."
  3. ./scripts/sign-extension.sh
Eklenti "unlisted" olarak imzalanır; mağazada yayınlanmaz, yalnızca sizin kurmanız için imzalı .xpi üretilir.
EOF
  exit 1
fi

command -v npx >/dev/null || { echo "Node.js gerekli: sudo apt install nodejs npm"; exit 1; }
mkdir -p "$OUT"
npx --yes web-ext@8 sign \
  --source-dir "$SRC/extension" \
  --artifacts-dir "$OUT" \
  --channel unlisted \
  --api-key "$AMO_JWT_ISSUER" \
  --api-secret "$AMO_JWT_SECRET"
echo "İmzalı eklenti: $OUT"
