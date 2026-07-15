#!/usr/bin/env bash
#
# build_mirror.sh — end-to-end build of the disconnected PyPI mirror.
# Run this on a CONNECTED machine. Steps:
#   1. resolve dependency closure from requirements.txt (local file or GitHub)
#   2. generate bandersnatch.conf
#   3. run bandersnatch to download wheels + build the /simple/ tree
#
# All configuration lives in config/settings.toml — edit nothing else.
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
emit("REQUIREMENTS_FILE", c["source"].get("requirements_file", ""))
emit("GITHUB_URL", c["source"].get("github_url", ""))
emit("PATH_IN_REPO", c["source"].get("requirements_path_in_repo", "requirements.txt"))
emit("CAP_LEVEL", c["versions"]["cap_level"])
emit("WINDOW_SIZE", c["versions"].get("window_size", 4))
emit("INCLUDE_PRE", "1" if c["versions"].get("include_prereleases") else "")
emit("PY_VERSIONS", c["targets"]["python_versions"])
emit("PLATFORMS", c["targets"]["platforms"])
emit("ARCHITECTURES", c["targets"].get("architectures", ["x86_64"]))
emit("ENSURE_PACKAGES", c.get("resolve", {}).get("ensure_packages", ["pip", "setuptools", "wheel"]))
emit("STRICT", "1" if c.get("resolve", {}).get("strict") else "")
emit("OUTPUT_DIR", c["mirror"]["output_dir"])
emit("MASTER", c["mirror"]["master"])
emit("WORKERS", c["mirror"]["workers"])
emit("KEEP_JSON", "1" if c["mirror"].get("keep_json", True) else "")
PY
}
eval "$(read_settings)"

if [ -z "$REQUIREMENTS_FILE" ] && [ -z "$GITHUB_URL" ]; then
    echo "ERROR: set source.requirements_file or source.github_url in $SETTINGS" >&2
    exit 1
fi

# Resolve paths relative to project root if not absolute.
abspath() { case "$1" in /*) echo "$1" ;; *) echo "$ROOT/${1#./}" ;; esac; }
OUTPUT_DIR="$(abspath "$OUTPUT_DIR")"

echo "==> [1/3] Resolving dependency closure"
# Ported ops fix: clear cached PyPI metadata so every build sees current data
# (prevents a stale resolver cache from producing a stale/incomplete mirror).
rm -rf "$WORK/.metacache"
SOURCE_ARGS=()
if [ -n "$REQUIREMENTS_FILE" ]; then
    REQUIREMENTS_FILE="$(abspath "$REQUIREMENTS_FILE")"
    SOURCE_ARGS=(--requirements-file "$REQUIREMENTS_FILE")
else
    SOURCE_ARGS=(--github-url "$GITHUB_URL" --path-in-repo "$PATH_IN_REPO")
fi
EXTRA_FLAGS=()
[ -n "$INCLUDE_PRE" ] && EXTRA_FLAGS+=(--include-prereleases)
[ -n "$STRICT" ] && EXTRA_FLAGS+=(--strict)
python3 "$HERE/resolve_deps.py" \
    "${SOURCE_ARGS[@]}" \
    --cap-level "$CAP_LEVEL" \
    --window-size "$WINDOW_SIZE" \
    --python-versions $PY_VERSIONS \
    --platforms $PLATFORMS \
    --architectures $ARCHITECTURES \
    --ensure-packages $ENSURE_PACKAGES \
    "${EXTRA_FLAGS[@]}" \
    --out-allowlist "$WORK/allowlist.txt" \
    --out-lock "$WORK/lock.json" \
    --out-report "$WORK/report.txt" \
    --cache-dir "$WORK/.metacache"

echo "==> [1b/3] Checking dependency-closure completeness"
python3 "$HERE/check_closure.py" "$WORK/lock.json" "$WORK/.metacache" || {
    echo "WARNING: closure check reported missing dependencies (see above)." >&2
    echo "         The mirror may be incomplete for a full offline install." >&2
}

echo "==> [2/3] Generating bandersnatch.conf"
KEEP_JSON_FLAG=(--keep-json)
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
if ! command -v bandersnatch >/dev/null 2>&1; then
    echo "ERROR: bandersnatch not installed. Run: pip install -r requirements-tooling.txt" >&2
    exit 1
fi
mkdir -p "$OUTPUT_DIR"
# Ported ops fix: drop bandersnatch's incremental state so a re-run does a full
# sync instead of skipping packages added to the allowlist since last time.
rm -f "$OUTPUT_DIR/status" "$OUTPUT_DIR/todo" 2>/dev/null || true
bandersnatch --config "$WORK/bandersnatch.conf" mirror

# Ship provenance with the mirror so the disconnected side can audit/verify.
mkdir -p "$OUTPUT_DIR/meta"
cp "$WORK/allowlist.txt" "$WORK/lock.json" "$WORK/report.txt" "$OUTPUT_DIR/meta/"
if [ -n "$REQUIREMENTS_FILE" ]; then
    cp "$REQUIREMENTS_FILE" "$OUTPUT_DIR/meta/requirements.txt"
fi

echo
echo "Done. Mirror is at: $OUTPUT_DIR"
echo
echo "  IMPORTANT: read $WORK/report.txt — it lists anything that could NOT"
echo "  be resolved and would be missing offline."
echo
echo "  Recommended: scripts/verify_mirror.sh   (per-Python pip dry-run in"
echo "               containers against this mirror; needs podman or docker)"
echo "  Then copy $OUTPUT_DIR to the disconnected host and run scripts/serve_mirror.sh"
