#!/usr/bin/env python3
"""resolve_deps.py — disconnected PyPI closure resolver (v2)."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

try:
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version, InvalidVersion
    from packaging.utils import canonicalize_name
except ImportError:
    sys.exit("Missing dependency. Run: pip install packaging --break-system-packages")

PYPI_JSON = "https://pypi.org/pypi/{name}/json"
USER_AGENT = "pypi-disconnected-resolver/2.0"
DEFAULT_ENSURE = ["pip", "setuptools", "wheel"]
FETCH_WORKERS = 16


def new_report() -> dict:
    return {
        "not_found": [],
        "unparsed": [],
        "unmirrorable": [],
        "conflicts": [],
        "sdist_only_no_meta": [],
        "py_relaxed": [],
        "marker_excluded": [],
    }


def report_has_problems(report: dict) -> bool:
    return any(report[k] for k in ("not_found", "unparsed", "unmirrorable"))


def format_report(report: dict, resolved_count: int) -> str:
    lines = ["=" * 70, "RESOLUTION REPORT", "=" * 70,
             f"Resolved packages: {resolved_count}", ""]

    def section(title, items, note=""):
        lines.append(f"{title} ({len(items)})" + (f" -- {note}" if note else ""))
        if items:
            for it in items:
                lines.append(f"  ! {it}")
        else:
            lines.append("  (none)")
        lines.append("")

    section("NOT FOUND ON PYPI -- these WILL BE MISSING from the mirror",
            report["not_found"], "check for typos (e.g. 'genism' vs 'gensim')")
    section("UNPARSEABLE LINES -- skipped", report["unparsed"])
    section("UNMIRRORABLE (editable/VCS/URL/local) -- handle manually",
            report["unmirrorable"])
    section("CONSTRAINT CONFLICTS -- mirrored newest instead (superset, pip "
            "will pick a working version)", report["conflicts"])
    section("SDIST-ONLY WITH NO DECLARED DEPS -- verify: hidden setup.py deps "
            "would be missing offline", report["sdist_only_no_meta"])
    section("PER-PYTHON RELAXATIONS -- an accumulated constraint had no version "
            "installable on this Python; mirrored the newest that IS "
            "(informational)", report["py_relaxed"])
    section("EXCLUDED BY TARGET MARKERS -- not needed on your targets",
            report["marker_excluded"])
    lines.append("=" * 70)
    return "\n".join(lines)


def to_raw_url(url: str, path_in_repo: str) -> str:
    url = url.strip().rstrip("/")
    if "raw.githubusercontent.com" in url:
        return url
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/blob/(.+)$", url)
    if m:
        owner, repo, rest = m.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{rest}"
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)$", url)
    if m:
        owner, repo = m.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{{branch}}/{path_in_repo}"
    return url


def http_get(url: str, retries: int = 3, timeout: int = 30) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except urllib.error.URLError as e:
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last}")


def fetch_requirements(github_url: str, path_in_repo: str):
    raw = to_raw_url(github_url, path_in_repo)
    if "{branch}" in raw:
        for branch in ("main", "master"):
            url = raw.format(branch=branch)
            try:
                return http_get(url).decode("utf-8"), url
            except urllib.error.HTTPError:
                continue
        raise RuntimeError(f"Could not find {path_in_repo} on main or master of {github_url}")
    return http_get(raw).decode("utf-8"), raw


def load_relative(ref: str, base):
    if re.match(r"https?://", ref):
        url = to_raw_url(ref, "requirements.txt")
        return http_get(url).decode("utf-8"), url
    if base and re.match(r"https?://", base):
        url = urllib.parse.urljoin(base, ref)
        return http_get(url).decode("utf-8"), url
    path = os.path.join(os.path.dirname(base), ref) if base else ref
    with open(path, encoding="utf-8") as f:
        return f.read(), path


_STRIP_OPTS = re.compile(r"\s--(?:hash|global-option|config-settings|install-option)[= ]\S+")
_URL_REQ = re.compile(r"^\S+\s*@\s*\S+")


def parse_requirements(text, base=None, report=None, _depth=0):
    if report is None:
        report = new_report()
    reqs = []
    text = re.sub(r"\\\r?\n", " ", text)
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-"):
            try:
                tokens = shlex.split(line)
            except ValueError:
                tokens = line.split()
            opt = tokens[0]
            if opt in ("-r", "--requirement", "-c", "--constraint") and len(tokens) > 1 and _depth < 10:
                ref = tokens[1]
                try:
                    loaded = load_relative(ref, base)
                except Exception as e:  # noqa: BLE001
                    print(f"  [warn] could not load include '{line}': {e}", file=sys.stderr)
                    report["unparsed"].append(f"{line}  (include failed: {e})")
                    continue
                if loaded:
                    sub_text, sub_base = loaded
                    print(f"  [info] following include: {ref}", file=sys.stderr)
                    reqs.extend(parse_requirements(sub_text, sub_base, report, _depth + 1))
                continue
            if opt in ("-e", "--editable"):
                print(f"  [warn] editable requirement cannot be mirrored: {line}", file=sys.stderr)
                report["unmirrorable"].append(line)
                continue
            print(f"  [warn] skipping unsupported requirements line: {line}", file=sys.stderr)
            report["unparsed"].append(line)
            continue
        if _URL_REQ.match(line) or line.startswith(("git+", "hg+", "svn+", "bzr+", "./", "/", "file:")):
            print(f"  [warn] URL/path requirement cannot be mirrored: {line}", file=sys.stderr)
            report["unmirrorable"].append(line)
            continue
        line = _STRIP_OPTS.sub("", line).strip()
        try:
            reqs.append(Requirement(line))
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] could not parse '{line}': {e}", file=sys.stderr)
            report["unparsed"].append(f"{line}  ({e})")
    return reqs


def lower_bound(spec):
    candidates = []
    for s in spec:
        if s.operator in (">=", "==", "~=", "==="):
            try:
                candidates.append(Version(s.version.replace(".*", ".0")))
            except InvalidVersion:
                pass
        elif s.operator == ">":
            try:
                candidates.append(Version(s.version))
            except InvalidVersion:
                pass
    return min(candidates) if candidates else None


def cap_for(v, cap_level):
    """Upper-bound token for the allowlist range.

    'none' (default) -> no upper bound: mirror every version from the floor
    FORWARD. 'major'/'minor' are optional size levers that cap the range.
    """
    if cap_level == "none":
        return ""
    rel = list(v.release) + [0, 0]
    if cap_level == "minor":
        return f"<{rel[0]}.{rel[1] + 1}"
    return f"<{rel[0] + 1}"


def build_specifier(floor, cap_level):
    cap = cap_for(floor, cap_level)
    return f">={floor},{cap}" if cap else f">={floor}"


PLATFORM_ENVS = {
    "linux": dict(sys_platform="linux", platform_system="Linux", os_name="posix"),
    "windows": dict(sys_platform="win32", platform_system="Windows", os_name="nt"),
    "macos": dict(sys_platform="darwin", platform_system="Darwin", os_name="posix"),
    "freebsd": dict(sys_platform="freebsd", platform_system="FreeBSD", os_name="posix"),
}

ARCH_MACHINES = {
    "x86_64": {"linux": "x86_64", "windows": "AMD64", "macos": "x86_64", "freebsd": "amd64"},
    "arm64": {"linux": "aarch64", "windows": "ARM64", "macos": "arm64", "freebsd": "arm64"},
}


def build_matrix(python_versions, platforms, architectures=None):
    architectures = architectures or ["x86_64"]
    envs = []
    for py in python_versions:
        parts = py.split(".")
        full = py if len(parts) >= 3 else f"{py}.0"
        for plat in platforms:
            for arch in architectures:
                base = dict(PLATFORM_ENVS[plat])
                base.update(
                    python_version=".".join(parts[:2]),
                    python_full_version=full,
                    implementation_name="cpython",
                    implementation_version=full,
                    platform_python_implementation="CPython",
                    platform_machine=ARCH_MACHINES[arch][plat],
                    platform_release="",
                    platform_version="",
                )
                envs.append(base)
    return envs


def marker_true_for_any(req, matrix, extras):
    if req.marker is None:
        return True
    extra_set = extras or {""}
    for env in matrix:
        for extra in extra_set:
            try:
                if req.marker.evaluate({**env, "extra": extra}):
                    return True
            except Exception:  # noqa: BLE001
                return True
    return False


_meta_cache = {}
_cache_lock = threading.Lock()


def _cached_json(cache_key, url, cache_dir, filename):
    with _cache_lock:
        if cache_key in _meta_cache:
            return _meta_cache[cache_key]
    cache_file = os.path.join(cache_dir, filename) if cache_dir else None
    if cache_file and os.path.exists(cache_file):
        with open(cache_file, encoding="utf-8") as f:
            data = json.load(f)
        with _cache_lock:
            _meta_cache[cache_key] = data
        return data
    try:
        raw = http_get(url)
        data = json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            data = None
        else:
            raise
    if data is not None and cache_file:
        tmp = cache_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, cache_file)
    with _cache_lock:
        _meta_cache[cache_key] = data
    return data


def pypi_metadata(name, cache_dir):
    key = canonicalize_name(name)
    return _cached_json(key, PYPI_JSON.format(name=key), cache_dir, f"{key}.json")


def pypi_metadata_version(name, version, cache_dir):
    key = canonicalize_name(name)
    return _cached_json(f"{key}=={version}",
                        f"https://pypi.org/pypi/{key}/{version}/json",
                        cache_dir, f"{key}-{version}.json")


def prefetch(jobs, fn, cache_dir):
    if not jobs:
        return
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        futures = [ex.submit(fn, *job, cache_dir) for job in jobs]
        for f in futures:
            try:
                f.result()
            except Exception:  # noqa: BLE001
                pass


def best_version(meta, spec, include_pre):
    versions = []
    for vstr, files in meta.get("releases", {}).items():
        if not files or all(f.get("yanked") for f in files):
            continue
        try:
            v = Version(vstr)
        except InvalidVersion:
            continue
        if v.is_prerelease and not include_pre:
            continue
        if spec.contains(v, prereleases=include_pre):
            versions.append(v)
    return max(versions) if versions else None


def release_requires_python(meta, vstr):
    """The requires_python string for a release (from its files), or None."""
    for f in (meta.get("releases", {}) or {}).get(vstr, []) or []:
        rp = f.get("requires_python")
        if rp:
            return rp
    return None


def python_ok(requires_python, py):
    """Is Python minor `py` (e.g. '3.9') admitted by a requires_python spec?

    Inclusive: true if ANY patch release of that minor is allowed. Unparseable
    or absent specs are treated as compatible (never leave a hole)."""
    if not requires_python:
        return True
    try:
        spec = SpecifierSet(requires_python)
    except Exception:  # noqa: BLE001
        return True
    for patch in (".0", ".5", ".99"):
        try:
            if spec.contains(Version(f"{py}{patch}"), prereleases=True):
                return True
        except Exception:  # noqa: BLE001
            return True
    return False


def best_version_for_python(meta, spec, include_pre, py):
    """Newest version satisfying `spec` AND installable on Python `py`
    (per its requires_python). This is what pip would pick on that Python."""
    best = None
    for vstr, files in meta.get("releases", {}).items():
        if not files or all(f.get("yanked") for f in files):
            continue
        try:
            v = Version(vstr)
        except InvalidVersion:
            continue
        if v.is_prerelease and not include_pre:
            continue
        if not spec.contains(v, prereleases=include_pre):
            continue
        if not python_ok(release_requires_python(meta, vstr), py):
            continue
        if best is None or v > best:
            best = v
    return best


def resolve(top_reqs, matrix, python_versions, cap_level, include_pre,
            cache_dir, report=None, verbose=False):
    """Resolve the closure so that EVERY target Python can install everything.

    For each package we select the newest version that is (a) allowed by the
    accumulated version constraints and (b) installable on Python `py`
    (requires_python) -- once PER target Python. Different Pythons may select
    different versions (e.g. numpy 2.x on 3.11+ but 1.26.x on 3.9); we walk the
    dependencies of EACH selected version, evaluating that version's markers
    against only the Pythons that use it. The allowlist then spans from the
    OLDEST selected version forward, so the mirror contains a working version
    for every interpreter."""
    if report is None:
        report = new_report()
    constraints = defaultdict(SpecifierSet)
    extras_req = defaultdict(set)
    required_by = defaultdict(set)
    is_top = set()
    floor_seen = {}                      # lowest version any SPECIFIER requests
    versions_by_pkg = defaultdict(set)   # name -> {all versions selected}
    picks_by_pkg = defaultdict(dict)     # name -> {version -> set(pythons)}

    queue = deque()
    for r in top_reqs:
        key = canonicalize_name(r.name)
        if r.marker is not None and not marker_true_for_any(r, matrix, set(r.extras)):
            report["marker_excluded"].append(str(r))
            continue
        is_top.add(key)
        constraints[key] &= r.specifier
        extras_req[key] |= set(r.extras)
        required_by[key].add("(requirements.txt)")
        fl = lower_bound(r.specifier)
        if fl and (key not in floor_seen or fl < floor_seen[key]):
            floor_seen[key] = fl
        queue.append(key)

    seen_states = set()   # (key, version, extras) already dep-walked
    known = set()         # keys ever processed

    while queue:
        wave = list(dict.fromkeys(queue))
        queue.clear()
        prefetch([(k,) for k in wave if k not in _meta_cache], pypi_metadata, cache_dir)

        newly = []  # (key, version, pythons, meta)
        for key in wave:
            known.add(key)
            meta = pypi_metadata(key, cache_dir)
            if meta is None:
                if key not in {canonicalize_name(x.split()[0]) for x in report["not_found"]}:
                    requesters = ", ".join(sorted(required_by[key])) or "?"
                    report["not_found"].append(f"{key}  (required by: {requesters})")
                    print(f"  [warn] {key} NOT FOUND on PyPI", file=sys.stderr)
                continue
            spec = constraints[key]
            picks = {}  # version -> set(py)
            for py in python_versions:
                v = best_version_for_python(meta, spec, include_pre, py)
                if v is None:
                    # No version under the accumulated constraints is installable
                    # on this Python (the constraint likely came from a path that
                    # only applies to OTHER Pythons). Fall back to the newest
                    # version that supports this Python so pip -- which resolves
                    # per interpreter -- can still install it here.
                    relaxed = best_version_for_python(meta, SpecifierSet(), include_pre, py)
                    if relaxed is not None:
                        note = f"{key} (py {py}): '{spec}' has no py{py} version; mirrored {relaxed}"
                        if note not in report["py_relaxed"]:
                            report["py_relaxed"].append(note)
                        v = relaxed
                if v is not None:
                    picks.setdefault(v, set()).add(py)
            if not picks:
                # Unsatisfiable on every target Python: keep newest, report it.
                v = best_version(meta, SpecifierSet(), include_pre) or \
                    best_version(meta, SpecifierSet(), True)
                if v is None:
                    report["not_found"].append(f"{key}  (no installable releases)")
                    continue
                requesters = ", ".join(sorted(required_by[key]))
                report["conflicts"].append(
                    f"{key}: '{spec}' unsatisfiable on targets "
                    f"(required by: {requesters}); using newest {v}")
                print(f"  [warn] {key}: '{spec}' unsatisfiable; falling back to {v}",
                      file=sys.stderr)
                picks = {v: set(python_versions)}

            for v, pys in picks.items():
                versions_by_pkg[key].add(v)
                picks_by_pkg[key].setdefault(v, set()).update(pys)
                state = (key, str(v), frozenset(extras_req[key]))
                if state in seen_states:
                    continue
                seen_states.add(state)
                newly.append((key, v, set(pys), meta))
            if verbose:
                pv = ", ".join(str(x) for x in sorted(picks))
                print(f"  resolved {key}: {pv}", file=sys.stderr)

        if not verbose and newly:
            nver = sum(len(x) for x in versions_by_pkg.values())
            print(f"\r  resolving... {len(versions_by_pkg)} packages / {nver} versions",
                  end="", file=sys.stderr)

        prefetch([(k, str(v)) for k, v, _, _ in newly if f"{k}=={v}" not in _meta_cache],
                 pypi_metadata_version, cache_dir)

        for key, v, pys, meta in newly:
            rd = (meta.get("info", {}) or {}).get("requires_dist")
            declared = rd is not None
            has_wheel = False
            per = pypi_metadata_version(key, str(v), cache_dir)
            if per:
                per_rd = (per.get("info", {}) or {}).get("requires_dist")
                if per_rd is not None:
                    rd = per_rd
                    declared = True
                else:
                    declared = False
                has_wheel = any(u.get("packagetype") == "bdist_wheel" for u in per.get("urls", []))
            if not has_wheel:
                files = (meta.get("releases", {}) or {}).get(str(v), [])
                has_wheel = any(f.get("packagetype") == "bdist_wheel" for f in files)
            if rd is None:
                rd = []
            if not declared and not has_wheel:
                tag = f"{key}=={v}"
                if tag not in report["sdist_only_no_meta"]:
                    report["sdist_only_no_meta"].append(tag)

            # This version's deps are evaluated against ONLY the Pythons using it.
            sub_matrix = [e for e in matrix if e["python_version"] in pys] or matrix
            for dep_str in rd:
                try:
                    dep = Requirement(dep_str)
                except Exception:  # noqa: BLE001
                    continue
                if not marker_true_for_any(dep, sub_matrix, extras_req[key]):
                    continue
                dkey = canonicalize_name(dep.name)
                before = (str(constraints[dkey]), frozenset(extras_req[dkey]))
                constraints[dkey] &= dep.specifier
                dfl = lower_bound(dep.specifier)
                if dfl and (dkey not in floor_seen or dfl < floor_seen[dkey]):
                    floor_seen[dkey] = dfl
                extras_req[dkey] |= set(dep.extras)
                required_by[dkey].add(f"{key}=={v}")
                after = (str(constraints[dkey]), frozenset(extras_req[dkey]))
                if dkey not in known or before != after:
                    queue.append(dkey)

    if not verbose:
        print(file=sys.stderr)

    allowlist = {}
    for key, vset in sorted(versions_by_pkg.items()):
        vmax = max(vset)
        # Floor = the OLDEST version any target Python needs (or any specifier
        # asks for), so every interpreter finds a working version. No upper cap
        # by default; 'major'/'minor' bound to the newest pick's series.
        floor = min([floor_seen.get(key, vmax)] + list(vset))
        cap = cap_for(vmax, cap_level)
        allowlist[key] = f">={floor},{cap}" if cap else f">={floor}"

    lock = {
        "packages": {
            key: {
                "resolved": str(max(versions_by_pkg[key])),
                "versions": [str(x) for x in sorted(versions_by_pkg[key])],
                "per_python": {str(v): sorted(picks_by_pkg[key][v])
                               for v in sorted(picks_by_pkg[key])},
                "allowlist_specifier": allowlist[key],
                "extras": sorted(extras_req[key]),
                "required_by": sorted(required_by[key]),
                "top_level": key in is_top,
            }
            for key in sorted(versions_by_pkg)
        },
        "count": len(versions_by_pkg),
        "report": report,
    }
    return allowlist, lock

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--github-url")
    ap.add_argument("--requirements-file")
    ap.add_argument("--path-in-repo", default="requirements.txt")
    ap.add_argument("--cap-level", default="none", choices=["none", "major", "minor"],
                    help="Upper bound on mirrored versions. 'none' (default) = from the "
                         "listed version forward, no cap. 'major'/'minor' shrink the mirror.")
    ap.add_argument("--python-versions", nargs="+", default=["3.9", "3.10", "3.11", "3.12", "3.13"])
    ap.add_argument("--platforms", nargs="+", default=["linux", "windows"])
    ap.add_argument("--architectures", nargs="+", default=["x86_64"], choices=list(ARCH_MACHINES))
    ap.add_argument("--ensure-packages", nargs="*", default=DEFAULT_ENSURE)
    ap.add_argument("--include-prereleases", action="store_true")
    ap.add_argument("--out-allowlist", default="allowlist.txt")
    ap.add_argument("--out-lock", default="lock.json")
    ap.add_argument("--out-report", default="report.txt")
    ap.add_argument("--cache-dir", default=".metacache")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    if args.cache_dir:
        os.makedirs(args.cache_dir, exist_ok=True)

    report = new_report()
    if args.requirements_file:
        with open(args.requirements_file, encoding="utf-8") as f:
            text = f.read()
        base = os.path.abspath(args.requirements_file)
    elif args.github_url:
        print(f"Fetching requirements from {args.github_url} ...", file=sys.stderr)
        text, base = fetch_requirements(args.github_url, args.path_in_repo)
    else:
        ap.error("provide --github-url or --requirements-file")

    top = parse_requirements(text, base=base, report=report)
    for name in args.ensure_packages or []:
        top.append(Requirement(name))
    print(f"Top-level requirements: {len(top)}", file=sys.stderr)
    matrix = build_matrix(args.python_versions, args.platforms, args.architectures)
    print(f"Marker environments: {len(matrix)}", file=sys.stderr)

    allowlist, lock = resolve(top, matrix, args.python_versions, args.cap_level,
                              args.include_prereleases, args.cache_dir,
                              report=report, verbose=args.verbose)

    with open(args.out_allowlist, "w", encoding="utf-8") as f:
        for name in sorted(allowlist):
            f.write(f"{name}{allowlist[name]}\n")
    lock["targets"] = {"python_versions": args.python_versions,
                       "platforms": args.platforms,
                       "architectures": args.architectures,
                       "cap_level": args.cap_level,
                       "include_prereleases": args.include_prereleases,
                       "ensure_packages": args.ensure_packages}
    with open(args.out_lock, "w", encoding="utf-8") as f:
        json.dump(lock, f, indent=2)

    text_report = format_report(report, lock["count"])
    with open(args.out_report, "w", encoding="utf-8") as f:
        f.write(text_report + "\n")
    print()
    print(text_report)
    print(f"\nResolved {lock['count']} packages.")
    if args.strict and report_has_problems(report):
        sys.exit(2)


if __name__ == "__main__":
    main()
