#!/usr/bin/env bash
#
# serve_mirror.sh — serve the mirror on the DISCONNECTED host.
#
# The bandersnatch output already contains a PEP 503 /simple/ tree under
# <mirror>/web/, so any static file server works. Two modes:
#
#   ./serve_mirror.sh static  [MIRROR_DIR] [PORT]   # plain HTTP, zero deps
#   ./serve_mirror.sh pypiserver [MIRROR_DIR] [PORT] # pypiserver (richer)
#
# Default: static on port 8080.
#
set -euo pipefail

MODE="${1:-static}"
MIRROR_DIR="${2:-./mirror}"
PORT="${3:-8080}"
WEB="$MIRROR_DIR/web"

if [ ! -d "$WEB/simple" ]; then
    echo "ERROR: $WEB/simple not found. Point MIRROR_DIR at the bandersnatch output." >&2
    exit 1
fi

case "$MODE" in
  static)
    echo "Serving $WEB at http://0.0.0.0:$PORT/simple/"
    echo "Clients:  pip install --index-url http://<host>:$PORT/simple/ <pkg>"
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
