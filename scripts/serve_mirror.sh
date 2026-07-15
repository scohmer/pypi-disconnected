#!/usr/bin/env bash
#
# serve_mirror.sh — serve the mirror on the DISCONNECTED host.
#
# Defaults (mode, port, mirror dir) come from config/settings.toml when it is
# present; positional args override:
#
#   ./serve_mirror.sh [MODE] [MIRROR_DIR] [PORT]
#     MODE: static (plain HTTP, zero deps) | pypiserver
#
# The bandersnatch output already contains a PEP 503 /simple/ tree under
# <mirror>/web/, so any static file server works.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SETTINGS="$ROOT/config/settings.toml"

# Defaults, optionally overridden by settings.toml, then by CLI args.
DEF_MODE="static"; DEF_DIR="./mirror"; DEF_PORT="8080"
if [ -f "$SETTINGS" ]; then
    eval "$(python3 - "$SETTINGS" <<'PY' 2>/dev/null || true
import sys, shlex
try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        sys.exit(0)  # stdlib-only host without tomllib: keep built-in defaults
with open(sys.argv[1], "rb") as f:
    c = tomllib.load(f)
s = c.get("serve", {})
print(f'DEF_MODE={shlex.quote(str(s.get("mode", "static")))}')
print(f'DEF_PORT={shlex.quote(str(s.get("port", 8080)))}')
print(f'DEF_DIR={shlex.quote(str(c.get("mirror", {}).get("output_dir", "./mirror")))}')
PY
)"
fi

MODE="${1:-$DEF_MODE}"
MIRROR_DIR="${2:-$DEF_DIR}"
PORT="${3:-$DEF_PORT}"
case "$MIRROR_DIR" in /*) : ;; *) MIRROR_DIR="$ROOT/${MIRROR_DIR#./}" ;; esac
WEB="$MIRROR_DIR/web"

if [ ! -d "$WEB/simple" ]; then
    echo "ERROR: $WEB/simple not found. Point MIRROR_DIR at the bandersnatch output." >&2
    exit 1
fi

case "$MODE" in
  static)
    echo "Serving $WEB at http://0.0.0.0:$PORT/simple/"
    echo "Clients:  pip install --index-url http://<host>:$PORT/simple/ --trusted-host <host> <pkg>"
    cd "$WEB"
    exec python3 -m http.server "$PORT"
    ;;
  pypiserver)
    if ! command -v pypi-server >/dev/null 2>&1; then
        echo "ERROR: pypiserver not installed. pip install pypiserver" >&2
        exit 1
    fi
    PKGS="$WEB/packages"
    echo "Serving $PKGS via pypiserver at http://0.0.0.0:$PORT/simple/"
    exec pypi-server run -p "$PORT" -a . -P . "$PKGS"
    ;;
  *)
    echo "Unknown mode: $MODE (use 'static' or 'pypiserver')" >&2
    exit 1
    ;;
esac
