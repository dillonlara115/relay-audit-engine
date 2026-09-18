#!/usr/bin/env bash
# Build app/static/console.css from app/styles/console.css with the Tailwind
# standalone CLI and the daisyUI plugin files in assets/. No Node involved.
# Pinned versions; bump both here and in the Dockerfile together.
set -euo pipefail
cd "$(dirname "$0")/.."
TW_VERSION="${TW_VERSION:-v4.3.3}"
BIN=".cache/tailwindcss"
if [ ! -x "$BIN" ]; then
  mkdir -p .cache
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)  asset=tailwindcss-linux-x64 ;;
    Linux-aarch64) asset=tailwindcss-linux-arm64 ;;
    Darwin-arm64)  asset=tailwindcss-macos-arm64 ;;
    Darwin-x86_64) asset=tailwindcss-macos-x64 ;;
    *) echo "unsupported platform" >&2; exit 1 ;;
  esac
  curl -sSL -o "$BIN" "https://github.com/tailwindlabs/tailwindcss/releases/download/$TW_VERSION/$asset"
  chmod +x "$BIN"
fi
"$BIN" -i app/styles/console.css -o app/static/console.css --minify
# Stamp the sources so a test can tell a stale build from a fresh one.
stamp=$(python3 - <<'PY'
import hashlib, pathlib
h = hashlib.sha256()
for p in sorted([pathlib.Path("app/styles/console.css"), *pathlib.Path("assets").glob("*.mjs"),
                 *pathlib.Path("app/console/templates").glob("*")]):
    h.update(p.name.encode()); h.update(p.read_bytes())
print(h.hexdigest()[:16])
PY
)
{ printf '/* built from %s */\n' "$stamp"; cat app/static/console.css; } > app/static/console.css.tmp
mv app/static/console.css.tmp app/static/console.css
echo "built app/static/console.css ($(wc -c < app/static/console.css) bytes, sources $stamp)"
