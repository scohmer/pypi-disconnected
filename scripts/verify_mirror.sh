#!/usr/bin/env bash
#
# verify_mirror.sh — prove the mirror can satisfy `pip install -r requirements.txt`
# WITHOUT touching pypi.org, before you carry it to the disconnected network.
#
# It serves the freshly built mirror on localhost, then asks pip to resolve the
# original requirements using ONLY that local index (--dry-run: nothing is
# installed). Names already flagged in report.txt (typos, unmirrorable lines)
# are excluded — they are known-missing and already reported.
#
# NOTE: pip resolves for the interpreter/OS it runs on. Run this on a machine
# matching one of your targets (or once per target in containers) for full
# coverage of every python_version/platform combination.
#
# Usage: scripts/verify_mirror.sh [config/settings.toml]
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SETTINGS="${1:-$ROOT/config/settings.toml}"
WORK="$ROOT/build"
PORT="${VERIFY_PORT:-8098}"

OUTPUT_DIR="$(python3 - "$SETTINGS" <<'PY'
import sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
with open(sys.argv[1], "rb") as f:
    c = tomllib.load(f)
print(c["mirror"]["output_dir"])
PY
)"
case "$OUTPUT_DIR" in /*) : ;; *) OUTPUT_DIR="$ROOT/${OUTPUT_DIR#./}" ;; esac

REQS="$OUTPUT_DIR/meta/requirements.txt"
LOCK="$OUTPUT_DIR/meta/lock.json"
[ -f "$REQS" ] || REQS="$WORK/../input/requirements.txt"
[ -f "$LOCK" ] || LOCK="$WORK/lock.json"
if [ ! -d "$OUTPUT_DIR/web/simple" ] || [ ! -f "$LOCK" ]; then
    echo "ERROR: mirror or lock.json not found — run scripts/build_mirror.sh first." >&2
    exit 1
fi

# Build a filtered requirements file: drop lines whose names are known-missing.
FILTERED="$WORK/verify-requirements.txt"
python3 - "$REQS" "$LOCK" "$FILTERED" <<'PY'
import json, re, sys
reqs, lock_path, out = sys.argv[1:4]
lock = json.load(open(lock_path))
rep = lock.get("report", {})
def canon(n): return re.sub(r"[-_.]+", "-", n).lower()
known_bad = {canon(e.split()[0]) for e in rep.get("not_found", [])}
kept, dropped = [], []
for line in open(reqs):
    s = line.split("#")[0].strip()
    if not s or s.startswith("-"):
        continue
    m = re.match(r"[A-Za-z0-9._-]+", s)
    (dropped if (m and canon(m.group(0)) in known_bad) else kept).append(s)
open(out, "w").write("\n".join(kept) + "\n")
if dropped:
    print(f"  [info] excluded {len(dropped)} known-missing name(s): {', '.join(dropped)}")
PY

echo "==> Serving mirror on 127.0.0.1:$PORT (local only)"
cd "$OUTPUT_DIR/web"
python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
sleep 1

echo "==> Asking pip to resolve everything from the mirror ONLY (--dry-run)"
if python3 -m pip install --dry-run --ignore-installed --no-cache-dir \
    --index-url "http://127.0.0.1:$PORT/simple/" \
    --report "$WORK/verify-report.json" \
    -r "$FILTERED"; then
    echo
    echo "PASS: pip resolved every requirement (and its dependencies) from the"
    echo "      mirror alone for $(python3 -V) / $(uname -s)."
    echo "      Full resolution: $WORK/verify-report.json"
else
    echo
    echo "FAIL: pip could NOT satisfy the requirements from the mirror alone." >&2
    echo "      The missing package is named in pip's error above." >&2
    exit 1
fi
