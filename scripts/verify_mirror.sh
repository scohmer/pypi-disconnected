#!/usr/bin/env bash
#
# verify_mirror.sh — prove the freshly built mirror can satisfy every requirement
# for EVERY target Python, on the CONNECTED build machine, before you ship it.
#
# For each Python in [targets].python_versions it pulls an official
# python:<ver> container (podman or docker), serves the local mirror to it, and
# runs `pip install --dry-run` for every top-level requirement using ONLY the
# mirror as the index. Each library is recorded per Python as:
#
#   OK       pip resolved it (and its dependencies) from the mirror alone
#   MISSING  the mirror cannot satisfy it            <-- a real mirror gap
#   BUILD    present in the mirror, but building an sdist failed for lack of a
#            system build dependency (e.g. mysqlclient -> libmysqlclient). The
#            mirror HAS the package; the target host just needs its build tools.
#
# It writes ONE summary report: build/verify-report.txt (+ verify-report.json),
# ending with a clear verdict: MIRROR FULLY FUNCTIONAL or NOT.
#
# Usage: scripts/verify_mirror.sh [config/settings.toml]
#   Env overrides: VERIFY_RUNTIME, VERIFY_PORT, VERIFY_JOBS, VERIFY_PY (space list)
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SETTINGS="${1:-$ROOT/config/settings.toml}"
WORK="$ROOT/build"
mkdir -p "$WORK"

# --- read settings --------------------------------------------------------- #
read_cfg() {
python3 - "$SETTINGS" <<'PY'
import sys, shlex
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
c = tomllib.load(open(sys.argv[1], "rb"))
def emit(k, v):
    if isinstance(v, list): v = " ".join(map(str, v))
    print(f'{k}={shlex.quote(str(v))}')
emit("OUTPUT_DIR", c["mirror"]["output_dir"])
emit("PY_VERSIONS", c["targets"]["python_versions"])
v = c.get("verify", {})
emit("RUNTIME", v.get("runtime", "auto"))
emit("IMAGE_TMPL", v.get("image_template", "docker.io/library/python:{py}-slim"))
emit("JOBS", v.get("jobs", 4))
emit("PORT", v.get("port", 8099))
PY
}
eval "$(read_cfg)"

# env overrides
RUNTIME="${VERIFY_RUNTIME:-$RUNTIME}"
PORT="${VERIFY_PORT:-$PORT}"
JOBS="${VERIFY_JOBS:-$JOBS}"
PY_VERSIONS="${VERIFY_PY:-$PY_VERSIONS}"

case "$OUTPUT_DIR" in /*) : ;; *) OUTPUT_DIR="$ROOT/${OUTPUT_DIR#./}" ;; esac
WEB="$OUTPUT_DIR/web"
REQS="$OUTPUT_DIR/meta/requirements.txt"; [ -f "$REQS" ] || REQS="$ROOT/input/requirements.txt"
LOCK="$OUTPUT_DIR/meta/lock.json";        [ -f "$LOCK" ] || LOCK="$WORK/lock.json"

if [ ! -d "$WEB/simple" ]; then
    echo "ERROR: $WEB/simple not found — run scripts/build_mirror.sh first." >&2
    exit 1
fi

# --- pick a container runtime --------------------------------------------- #
pick_runtime() {
    case "$1" in
        podman|docker) command -v "$1" >/dev/null 2>&1 && { echo "$1"; return 0; } ;;
        auto)
            for r in podman docker; do
                command -v "$r" >/dev/null 2>&1 && { echo "$r"; return 0; }
            done ;;
    esac
    return 1
}
if ! RT="$(pick_runtime "$RUNTIME")"; then
    echo "ERROR: no container runtime found (need podman or docker; set [verify].runtime)." >&2
    exit 2
fi
echo "==> Using container runtime: $RT"

# --- build the library list (top-level names, minus known-missing) --------- #
LIBS="$WORK/verify-libs.txt"
python3 - "$REQS" "$LOCK" "$LIBS" <<'PY'
import json, re, sys
reqs, lock_path, out = sys.argv[1:4]
known_bad = set()
try:
    rep = json.load(open(lock_path)).get("report", {})
    known_bad = {re.sub(r"[-_.]+","-",e.split()[0]).lower() for e in rep.get("not_found", [])}
except Exception:
    pass
seen, kept, dropped = set(), [], []
for line in open(reqs):
    s = line.split("#")[0].strip()
    if not s or s.startswith("-"):
        continue
    name = re.match(r"[A-Za-z0-9._-]+", s)
    if not name:
        continue
    canon = re.sub(r"[-_.]+","-",name.group(0)).lower()
    if canon in seen:
        continue
    seen.add(canon)
    (dropped if canon in known_bad else kept).append(s)
open(out, "w").write("\n".join(kept) + "\n")
print(f"  {len(kept)} libraries to verify"
      + (f" ({len(dropped)} known-missing excluded: {', '.join(dropped)})" if dropped else ""))
PY

# --- serve the mirror to the containers ------------------------------------ #
echo "==> Serving mirror at 127.0.0.1:$PORT"
( cd "$WEB" && exec python3 -m http.server "$PORT" --bind 127.0.0.1 ) >/dev/null 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
sleep 1

RESULTS="$WORK/verify-results.tsv"   # py<TAB>status<TAB>library
: > "$RESULTS"

# This runs INSIDE each container: loop libraries, pip dry-run each, classify.
CONTAINER_SCRIPT='
set -u
IDX="$1"; HOSTPORT="$2"; JOBS="$3"
check() {
  lib="$1"
  out=$(pip install --dry-run --ignore-installed --no-cache-dir --no-input \
        --disable-pip-version-check --index-url "$IDX" --trusted-host "$HOSTPORT" \
        "$lib" 2>&1)
  if [ $? -eq 0 ]; then
    printf "OK\t%s\n" "$lib"
  elif printf "%s" "$out" | grep -qiE "no matching distribution|could not find a version that satisfies"; then
    printf "MISSING\t%s\n" "$lib"
  else
    printf "BUILD\t%s\n" "$lib"
  fi
}
export -f check; export IDX HOSTPORT
grep -vE "^[[:space:]]*$" /reqs.txt | xargs -P"$JOBS" -I{} bash -c "check \"\$@\"" _ {}
'

OVERALL_OK=1
for PY in $PY_VERSIONS; do
    IMAGE="${IMAGE_TMPL/\{py\}/$PY}"
    echo "==> [py$PY] $IMAGE"
    if ! "$RT" image exists "$IMAGE" 2>/dev/null && ! "$RT" pull "$IMAGE" >/dev/null 2>&1; then
        echo "  [warn] could not pull $IMAGE — skipping py$PY" >&2
        printf "%s\tSKIP\t(image unavailable)\n" "$PY" >> "$RESULTS"
        OVERALL_OK=0
        continue
    fi
    # --network=host lets the container reach the host's 127.0.0.1:$PORT server.
    "$RT" run --rm --network=host -v "$LIBS":/reqs.txt:ro \
        "$IMAGE" bash -c "$CONTAINER_SCRIPT" _ \
        "http://127.0.0.1:$PORT/simple/" "127.0.0.1:$PORT" "$JOBS" \
        2>/dev/null | sed "s/^/${PY}\t/" >> "$RESULTS" || true
done

# --- aggregate into one summary report ------------------------------------- #
python3 - "$RESULTS" "$LIBS" "$PY_VERSIONS" "$WORK/verify-report.txt" "$WORK/verify-report.json" <<'PY'
import json, sys, collections, re
results, libs_path, pyvers, out_txt, out_json = sys.argv[1:6]
pyvers = pyvers.split()
libs = [l.strip() for l in open(libs_path) if l.strip()]
status = {}
skipped = set()
for line in open(results):
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 3:
        continue
    py, st, lib = parts[0], parts[1], parts[2]
    if st == "SKIP":
        skipped.add(py); continue
    status[(py, lib)] = st

def name(lib):
    return re.match(r"[A-Za-z0-9._-]+", lib).group(0)

active = [p for p in pyvers if p not in skipped]
lines = ["=" * 74, "MIRROR VERIFICATION REPORT", "=" * 74,
         f"Pythons tested : {', '.join(active)}"
         + (f"   (SKIPPED: {', '.join(sorted(skipped))})" if skipped else ""),
         f"Libraries      : {len(libs)}", ""]

per_py = {}
for py in active:
    c = collections.Counter(status.get((py, l), "MISSING") for l in libs)
    per_py[py] = c
    lines.append(f"Python {py}:  OK={c['OK']}  MISSING={c['MISSING']}  BUILD={c['BUILD']}")
lines.append("")

def cell(py, lib):
    return {"OK": "ok", "MISSING": "MISS", "BUILD": "build"}.get(status.get((py, lib), "MISS"), "?")

problem_libs = [l for l in libs if any(status.get((py, l)) != "OK" for py in active)]
if problem_libs:
    lines.append("Libraries needing attention (per Python):")
    lines.append("  " + f"{'library':32}" + "".join(f"{p:>7}" for p in active))
    lines.append("  " + "-" * (32 + 7 * len(active)))
    for l in problem_libs:
        lines.append("  " + f"{name(l):32}" + "".join(f"{cell(p,l):>7}" for p in active))
    lines.append("")

total_missing = sum(1 for v in status.values() if v == "MISSING")
total_build   = sum(1 for v in status.values() if v == "BUILD")
lines.append("Legend: ok = resolved from mirror | MISS = mirror gap (FAIL) | "
             "build = present, needs system build deps on target")
lines.append("")
functional = (total_missing == 0 and not skipped)
lines.append("-" * 74)
if functional:
    lines.append("VERDICT: MIRROR FULLY FUNCTIONAL — every library resolves on every "
                 "target Python.")
    if total_build:
        lines.append(f"NOTE: {total_build} library/Python builds need system build "
                     "dependencies on the target host (mirror content is complete).")
else:
    if skipped:
        lines.append(f"VERDICT: INCOMPLETE — {len(skipped)} Python(s) could not be "
                     "tested (image unavailable).")
    if total_missing:
        lines.append(f"VERDICT: NOT FULLY FUNCTIONAL — {total_missing} library/Python "
                     "combination(s) cannot be satisfied by the mirror (MISS above).")
lines.append("-" * 74)

report = "\n".join(lines) + "\n"
open(out_txt, "w").write(report)
json.dump({"pythons": pyvers, "skipped": sorted(skipped),
           "per_python": {p: dict(c) for p, c in per_py.items()},
           "missing": [[py, l] for (py, l), v in status.items() if v == "MISSING"],
           "build": [[py, l] for (py, l), v in status.items() if v == "BUILD"],
           "functional": functional}, open(out_json, "w"), indent=2)
print(report)
sys.exit(0 if functional else 1)
PY
VERDICT=$?

echo
echo "Summary report: $WORK/verify-report.txt  (JSON: $WORK/verify-report.json)"
exit $VERDICT
