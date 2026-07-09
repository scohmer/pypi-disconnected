#!/usr/bin/env python3
"""
resolve_deps.py
===============
Given a GitHub link to a requirements.txt, build the full transitive dependency
closure and emit:

  * allowlist.txt  - PEP 440 version specifiers, one per line, for the
                     bandersnatch [allowlist] packages section.
  * lock.json      - a reproducible manifest (resolved versions, ranges,
                     who-required-what, target matrix) for auditing / re-runs.

Strategy
--------
1. Fetch requirements.txt (repo URL, blob URL, or raw URL all accepted).
2. For each top-level requirement, the lower bound is the version LISTED in
   requirements.txt. If a top-level requirement has NO version listed at all,
   the lower bound instead becomes the oldest release that still supports the
   lowest Python version in [targets] python_versions -- otherwise an unpinned
   entry would collapse to "whatever is newest today", which may have already
   dropped support for an older Python you still target.
3. Walk transitive dependencies using the PyPI JSON API. For each package we
   pick the newest version satisfying the accumulated constraints, read its
   `requires_dist`, and evaluate environment markers across the target matrix
   (python_versions x platforms). A dependency is followed if its marker is
   true for ANY target environment. Extras are followed only when requested.
   For transitive packages the lower bound is the resolved version.
4. Every allowlist entry is left open-ended (no upper bound): the mirror
   carries every release from the floor through whatever is newest at build
   time, so bandersnatch (and future re-runs) naturally pick up new releases.

This script needs network access to PyPI (run it on the CONNECTED machine).
It only reads metadata here; bandersnatch does the actual file downloads.

Stdlib + `packaging` only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque

try:
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version, InvalidVersion
    from packaging.markers import UndefinedEnvironmentName
    from packaging.utils import canonicalize_name
except ImportError:
    sys.exit("Missing dependency. Run: pip install packaging --break-system-packages")

PYPI_JSON = "https://pypi.org/pypi/{name}/json"
USER_AGENT = "pypi-disconnected-resolver/1.0"


# --------------------------------------------------------------------------- #
# Fetching requirements.txt from GitHub
# --------------------------------------------------------------------------- #
def to_raw_url(url: str, path_in_repo: str) -> str:
    """Normalize a GitHub URL (repo / blob / raw) to a raw.githubusercontent URL."""
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
        # Caller resolves branch; return a template marker handled in fetch_requirements.
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{{branch}}/{path_in_repo}"
    return url  # assume it is already a direct URL


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


def fetch_requirements(github_url: str, path_in_repo: str) -> str:
    raw = to_raw_url(github_url, path_in_repo)
    if "{branch}" in raw:
        for branch in ("main", "master"):
            try:
                return http_get(raw.format(branch=branch)).decode("utf-8")
            except urllib.error.HTTPError:
                continue
        raise RuntimeError(f"Could not find {path_in_repo} on main or master of {github_url}")
    return http_get(raw).decode("utf-8")


def parse_requirements(text: str) -> list[Requirement]:
    reqs = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-"):
            # -r/-c/-e and other pip flags: skip with a warning.
            print(f"  [warn] skipping unsupported requirements line: {line}", file=sys.stderr)
            continue
        line = line.split(";")[0].strip() + (";" + line.split(";", 1)[1] if ";" in line else "")
        try:
            reqs.append(Requirement(line))
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] could not parse '{line}': {e}", file=sys.stderr)
    return reqs


# --------------------------------------------------------------------------- #
# Version range helpers
# --------------------------------------------------------------------------- #
def lower_bound(spec: SpecifierSet) -> Version | None:
    """Smallest version permitted by a specifier set (the 'listed' floor)."""
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


def build_specifier(floor: Version) -> str:
    return f">={floor}"


# --------------------------------------------------------------------------- #
# Target environment matrix (for marker evaluation)
# --------------------------------------------------------------------------- #
PLATFORM_ENVS = {
    "linux": dict(sys_platform="linux", platform_system="Linux", os_name="posix"),
    "windows": dict(sys_platform="win32", platform_system="Windows", os_name="nt"),
    "macos": dict(sys_platform="darwin", platform_system="Darwin", os_name="posix"),
    "freebsd": dict(sys_platform="freebsd", platform_system="FreeBSD", os_name="posix"),
}


def build_matrix(python_versions: list[str], platforms: list[str]) -> list[dict]:
    envs = []
    for py in python_versions:
        parts = py.split(".")
        full = py if len(parts) >= 3 else f"{py}.0"
        for plat in platforms:
            base = dict(PLATFORM_ENVS[plat])
            base.update(
                python_version=".".join(parts[:2]),
                python_full_version=full,
                implementation_name="cpython",
                platform_python_implementation="CPython",
            )
            envs.append(base)
    return envs


def marker_true_for_any(req: Requirement, matrix: list[dict], extras: set[str]) -> bool:
    if req.marker is None:
        return True
    extra_set = extras or {""}
    for env in matrix:
        for extra in extra_set:
            try:
                if req.marker.evaluate({**env, "extra": extra}):
                    return True
            except UndefinedEnvironmentName:
                continue
    return False


# --------------------------------------------------------------------------- #
# PyPI metadata
# --------------------------------------------------------------------------- #
_meta_cache: dict[str, dict] = {}


def pypi_metadata(name: str, cache_dir: str | None) -> dict | None:
    key = canonicalize_name(name)
    if key in _meta_cache:
        return _meta_cache[key]
    cache_file = os.path.join(cache_dir, f"{key}.json") if cache_dir else None
    if cache_file and os.path.exists(cache_file):
        with open(cache_file, encoding="utf-8") as f:
            data = json.load(f)
        _meta_cache[key] = data
        return data
    try:
        raw = http_get(PYPI_JSON.format(name=key))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"  [warn] {name} not found on PyPI", file=sys.stderr)
            _meta_cache[key] = None
            return None
        raise
    data = json.loads(raw)
    if cache_file:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(data, f)
    _meta_cache[key] = data
    return data


def pypi_metadata_version(name: str, version: str, cache_dir: str | None) -> dict | None:
    key = canonicalize_name(name)
    ck = f"{key}=={version}"
    if ck in _meta_cache:
        return _meta_cache[ck]
    cache_file = os.path.join(cache_dir, f"{key}-{version}.json") if cache_dir else None
    if cache_file and os.path.exists(cache_file):
        with open(cache_file, encoding="utf-8") as f:
            data = json.load(f)
        _meta_cache[ck] = data
        return data
    url = f"https://pypi.org/pypi/{key}/{version}/json"
    try:
        raw = http_get(url)
    except urllib.error.HTTPError:
        _meta_cache[ck] = None
        return None
    data = json.loads(raw)
    if cache_file:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(data, f)
    _meta_cache[ck] = data
    return data


def best_version(meta: dict, spec: SpecifierSet, include_pre: bool) -> Version | None:
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


def release_supports_python(files: list[dict], python_full_version: str) -> bool:
    """True if any file in this release is installable under python_full_version.

    A file with no requires_python metadata is treated as unconstrained (common
    for releases published before python_requires existed).
    """
    if not files:
        return False
    target = Version(python_full_version)
    for f in files:
        rp = f.get("requires_python")
        if not rp:
            return True
        try:
            if SpecifierSet(rp).contains(target, prereleases=True):
                return True
        except Exception:  # noqa: BLE001 - malformed requires_python on PyPI
            continue
    return False


def min_version_supporting_python(meta: dict, python_full_version: str, include_pre: bool) -> Version | None:
    """Oldest non-yanked release that supports python_full_version."""
    candidates = []
    for vstr, files in meta.get("releases", {}).items():
        if not files or all(f.get("yanked") for f in files):
            continue
        try:
            v = Version(vstr)
        except InvalidVersion:
            continue
        if v.is_prerelease and not include_pre:
            continue
        if release_supports_python(files, python_full_version):
            candidates.append(v)
    return min(candidates) if candidates else None


def oldest_full_version(python_versions: list[str]) -> str:
    """Full version string (X.Y.Z) of the lowest entry in python_versions."""
    oldest = min(python_versions, key=lambda p: tuple(int(x) for x in p.split(".")))
    return oldest if oldest.count(".") >= 2 else f"{oldest}.0"


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def resolve(top_reqs, matrix, include_pre, cache_dir, python_versions, verbose=False):
    # constraints[name] = accumulated SpecifierSet
    constraints: dict[str, SpecifierSet] = defaultdict(SpecifierSet)
    extras_req: dict[str, set] = defaultdict(set)
    required_by: dict[str, set] = defaultdict(set)
    is_top: set[str] = set()
    listed_floor: dict[str, Version] = {}
    # Auto floor for top-level requirements with NO version listed at all: the
    # oldest release that still supports the lowest configured Python version,
    # mirrored uncapped through latest (see min_version_supporting_python).
    python_floor: dict[str, Version] = {}
    oldest_py = oldest_full_version(python_versions)

    queue: deque[str] = deque()
    for r in top_reqs:
        key = canonicalize_name(r.name)
        is_top.add(key)
        constraints[key] &= r.specifier
        extras_req[key] |= set(r.extras)
        required_by[key].add("(requirements.txt)")
        fl = lower_bound(r.specifier)
        if fl:
            listed_floor[key] = fl
        else:
            meta = pypi_metadata(key, cache_dir)
            if meta:
                pf = min_version_supporting_python(meta, oldest_py, include_pre)
                if pf:
                    python_floor[key] = pf
        queue.append(key)

    resolved: dict[str, Version] = {}
    seen_states: set = set()

    while queue:
        key = queue.popleft()
        meta = pypi_metadata(key, cache_dir)
        if meta is None:
            continue
        spec = constraints[key]
        v = best_version(meta, spec, include_pre)
        if v is None:
            print(f"  [warn] no version of {key} satisfies {spec}", file=sys.stderr)
            continue
        state = (key, str(v), frozenset(extras_req[key]))
        if state in seen_states:
            continue
        seen_states.add(state)
        resolved[key] = v
        if verbose:
            print(f"  resolved {key}=={v}", file=sys.stderr)
        else:
            print(f"\r  resolving... {len(resolved)} packages", end="", file=sys.stderr)

        # info.requires_dist on the project endpoint reflects the LATEST version,
        # so re-fetch the per-version JSON to get requires_dist for the version we
        # actually resolved.
        rd = (meta.get("info", {}) or {}).get("requires_dist") or []
        per = pypi_metadata_version(key, str(v), cache_dir)
        if per:
            rd = (per.get("info", {}) or {}).get("requires_dist") or rd

        for dep_str in rd:
            try:
                dep = Requirement(dep_str)
            except Exception:  # noqa: BLE001
                continue
            if not marker_true_for_any(dep, matrix, extras_req[key]):
                continue
            dkey = canonicalize_name(dep.name)
            before = (str(constraints[dkey]), frozenset(extras_req[dkey]))
            constraints[dkey] &= dep.specifier
            extras_req[dkey] |= set(dep.extras)
            required_by[dkey].add(f"{key}=={v}")
            after = (str(constraints[dkey]), frozenset(extras_req[dkey]))
            if dkey not in resolved or before != after:
                queue.append(dkey)

    if not verbose:
        print(file=sys.stderr)  # end the \r progress line

    # Build final allowlist specifiers: floor forward through latest, uncapped.
    #   - explicit version in requirements.txt -> that version
    #   - unlisted top-level requirement       -> oldest release supporting the
    #                                              lowest configured Python
    #   - transitive dependency                -> the resolved version
    allowlist = {}
    for key, v in sorted(resolved.items()):
        floor = listed_floor.get(key, python_floor.get(key, v))
        floor = min(floor, v)
        allowlist[key] = build_specifier(floor)

    lock = {
        "packages": {
            key: {
                "resolved": str(resolved[key]),
                "allowlist_specifier": allowlist[key],
                "extras": sorted(extras_req[key]),
                "required_by": sorted(required_by[key]),
                "top_level": key in is_top,
            }
            for key in sorted(resolved)
        },
        "count": len(resolved),
    }
    return allowlist, lock


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Resolve a PyPI dependency closure for a disconnected mirror.")
    ap.add_argument("--github-url", required=False, help="Repo / blob / raw URL to requirements.txt")
    ap.add_argument("--requirements-file", help="Local requirements.txt instead of fetching")
    ap.add_argument("--path-in-repo", default="requirements.txt")
    ap.add_argument("--python-versions", nargs="+", default=["3.9", "3.10", "3.11", "3.12"])
    ap.add_argument("--platforms", nargs="+", default=["linux", "windows"])
    ap.add_argument("--include-prereleases", action="store_true")
    ap.add_argument("--out-allowlist", default="allowlist.txt")
    ap.add_argument("--out-lock", default="lock.json")
    ap.add_argument("--cache-dir", default=".metacache")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print each resolved package to stderr (instead of a compact counter).")
    args = ap.parse_args()

    if args.cache_dir:
        os.makedirs(args.cache_dir, exist_ok=True)

    if args.requirements_file:
        with open(args.requirements_file, encoding="utf-8") as f:
            text = f.read()
    elif args.github_url:
        print(f"Fetching requirements from {args.github_url} ...", file=sys.stderr)
        text = fetch_requirements(args.github_url, args.path_in_repo)
    else:
        ap.error("provide --github-url or --requirements-file")

    top = parse_requirements(text)
    print(f"Top-level requirements: {len(top)}", file=sys.stderr)
    matrix = build_matrix(args.python_versions, args.platforms)

    allowlist, lock = resolve(top, matrix, args.include_prereleases,
                              args.cache_dir, args.python_versions, verbose=args.verbose)

    with open(args.out_allowlist, "w", encoding="utf-8") as f:
        for name in sorted(allowlist):
            f.write(f"{name}{allowlist[name]}\n")
    lock["targets"] = {"python_versions": args.python_versions, "platforms": args.platforms,
                       "include_prereleases": args.include_prereleases}
    with open(args.out_lock, "w", encoding="utf-8") as f:
        json.dump(lock, f, indent=2)

    print(f"\nResolved {lock['count']} packages.")
    print(f"  allowlist -> {args.out_allowlist}")
    print(f"  lock      -> {args.out_lock}")


if __name__ == "__main__":
    main()
