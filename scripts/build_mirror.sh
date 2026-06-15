#!/usr/bin/env bash
#
# build_mirror.sh — end-to-end build of the disconnected PyPI mirror.
# Run this on a CONNECTED machine. Steps:
#   1. resolve dependency closure from the GitHub requirements.txt
#   2. generate bandersnatch.conf
#   3. run bandersnatch to download wheels + build the /simple/ tree
#
# Usage:  scripts/build_mirror.sh [config/settings.toml]
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SETTINGS="${1:-$ROOT/config/settings.toml}"
WORK="$ROOT/build"
mkdir -p "$WORK"

# --- read settings.toml into shell variables (needs python3.11+ for tomllib) ---
read_settings() {
python3 - "$SETTINGS" <<'PY'
import sys, shlex
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # pip install tomli on <3.11
with open(sys.argv[1], "rb") as f:
    c = tomllib.load(f)
def emit(k, v):
    if isinstance(v, list):
        v = " ".join(map(str, v))
    print(f'{k}={shlex.quote(str(v))}')
emit("GITHUB_URL", c["source"]["github_url"])
emit("PATH_IN_REPO", c["source"].get("requirements_path_in_repo", "requirements.txt"))
emit("CAP_LEVEL", c["versions"]["cap_level"])
emit("INCLUDE_PRE", "1" if c["versions"].get("include_prereleases") else "")
emit("PY_VERSIONS", c["targets"]["python_versions"])
emit("PLATFORMS", c["targets"]["platforms"])
emit("OUTPUT_DIR", c["mirror"]["output_dir"])
emit("MASTER", c["mirror"]["master"])
emit("WORKERS", c["mirror"]["workers"])
emit("KEEP_JSON", "1" if c["mirror"].get("keep_json", True) else "")
PY
}
eval "$(read_settings)"

# Resolve OUTPUT_DIR relative to project root if not absolute.
case "$OUTPUT_DIR" in
    /*) : ;;
    *) OUTPUT_DIR="$ROOT/${OUTPUT_DIR#./}" ;;
esac

echo "==> [1/3] Resolving dependency closure"
rm -rf "$WORK/.metacache"
PRE_FLAG=()
[ -n "$INCLUDE_PRE" ] && PRE_FLAG=(--include-prereleases)
python3 "$HERE/resolve_deps.py" \
    --github-url "$GITHUB_URL" \
    --path-in-repo "$PATH_IN_REPO" \
    --cap-level "$CAP_LEVEL" \
    --python-versions $PY_VERSIONS \
    --platforms $PLATFORMS \
    "${PRE_FLAG[@]}" \
    --out-allowlist "$WORK/allowlist.txt" \
    --out-lock "$WORK/lock.json" \
    --cache-dir "$WORK/.metacache"

echo "==> [2/3] Generating bandersnatch.conf"
KEEP_JSON_FLAG=()
[ -z "$KEEP_JSON" ] && KEEP_JSON_FLAG=(--no-keep-json)
python3 "$HERE/generate_bandersnatch_conf.py" \
    --allowlist "$WORK/allowlist.txt" \
    --output-dir "$OUTPUT_DIR" \
    --master "$MASTER" \
    --workers "$WORKERS" \
    --platforms $PLATFORMS \
    --python-versions $PY_VERSIONS \
    "${KEEP_JSON_FLAG[@]}" \
    --out "$WORK/bandersnatch.conf"

echo "==> [3/3] Running bandersnatch mirror"
rm -f "$OUTPUT_DIR/status"
if ! command -v bandersnatch >/dev/null 2>&1; then
    echo "ERROR: bandersnatch not installed. Run: pip install -r requirements-tooling.txt" >&2
    exit 1
fi
mkdir -p "$OUTPUT_DIR"
bandersnatch --config "$WORK/bandersnatch.conf" mirror

echo
echo "Done. Mirror is at: $OUTPUT_DIR"
echo "Copy that directory to the disconnected host and run scripts/serve_mirror.sh there."
