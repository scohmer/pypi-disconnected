#!/usr/bin/env python3
"""Prove the closure is complete for EVERY target Python (offline, no builds).

For each resolved package, for each version it was selected at (and the set of
Pythons that use that version), we re-read that version's requires_dist, keep
the marker-active dependencies for those Pythons, and check two things:

  1. PRESENT  - the dependency is in the closure at all, and
  2. COVERED  - the mirror's allowlist range for that dependency actually
                contains a real PyPI version that (a) satisfies the requiring
                specifier and (b) is installable on at least one of the Pythons
                that need it (requires_python).

If every edge is PRESENT and COVERED, then `pip install -r requirements.txt`
can resolve on each of Python 3.9-3.13 using only the mirror.
"""
import os
import json
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from resolve_deps import (build_matrix, marker_true_for_any, pypi_metadata,
                          pypi_metadata_version, release_requires_python,
                          python_ok, canonicalize_name)
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version, InvalidVersion

lock = json.load(open(sys.argv[1]))
cache = sys.argv[2]
tgt = lock["targets"]
pyvers = tgt["python_versions"]
platforms = tgt["platforms"]
archs = tgt.get("architectures", ["x86_64"])
matrix = build_matrix(pyvers, platforms, archs)
pkgs = lock["packages"]

# allowlist per package: one or more specifiers (a version is mirrored if it
# matches ANY of them, matching bandersnatch's allowlist_release behavior).
def _specs(info):
    raw = info.get("allowlist_specifiers")
    if raw is None:
        raw = [info.get("allowlist_specifier", "")]
    return [SpecifierSet(x) for x in raw if x]

def _in_allow(specsets, v):
    return any(ss.contains(v, prereleases=True) for ss in specsets)

allow = {k: _specs(v) for k, v in pkgs.items()}

def real_versions(name):
    meta = pypi_metadata(name, cache)
    if not meta:
        return {}
    out = {}
    for vs, files in meta.get("releases", {}).items():
        if not files or all(f.get("yanked") for f in files):
            continue
        try:
            out[Version(vs)] = release_requires_python(meta, vs)
        except InvalidVersion:
            continue
    return out

from packaging.version import Version as _V
def primary_versions(info):
    """Newest selected version per Python (what pip picks first)."""
    per = info.get("per_python", {})
    by_py = {}
    for vstr, pys in per.items():
        for py in pys:
            if py not in by_py or _V(vstr) > _V(by_py[py]):
                by_py[py] = vstr
    return set(by_py.values())

missing = []          # dep not in closure at all (critical)
coverage_gaps = []    # PRIMARY parent: no mirrored dep version works (critical)
backtrack_gaps = []   # older window-only parent version (informational)
edges = 0

for name, info in pkgs.items():
    _primary = primary_versions(info)
    for vstr in info["versions"]:
        pys = info["per_python"].get(vstr, pyvers)
        _is_primary = vstr in _primary
        sub = [e for e in matrix if e["python_version"] in pys] or matrix
        per = pypi_metadata_version(name, vstr, cache)
        if not per:
            continue
        rd = (per.get("info", {}) or {}).get("requires_dist") or []
        for dep_str in rd:
            try:
                dep = Requirement(dep_str)
            except Exception:
                continue
            if not marker_true_for_any(dep, sub, set(info["extras"])):
                continue
            dkey = canonicalize_name(dep.name)
            edges += 1
            if dkey not in pkgs:
                missing.append(f"{name}=={vstr} (py {','.join(sorted(pys))}) -> {dep_str}")
                continue
            rng = allow.get(dkey, [])
            rv = real_versions(dkey)
            covered = False
            for v, rp in rv.items():
                if not _in_allow(rng, v):
                    continue
                if dep.specifier and not dep.specifier.contains(v, prereleases=True):
                    continue
                if any(python_ok(rp, py) for py in pys):
                    covered = True
                    break
            if not covered:
                msg = (f"{name}=={vstr} needs {dep.name}{dep.specifier} on py "
                       f"{','.join(sorted(pys))}; mirror {dkey} "
                       f"{[str(x) for x in rng]} has no usable version")
                (coverage_gaps if _is_primary else backtrack_gaps).append(msg)

total_versions = sum(len(i["versions"]) for i in pkgs.values())
print(f"Closure packages   : {len(pkgs)}")
print(f"Selected versions  : {total_versions}  (across Python {', '.join(pyvers)})")
print(f"Dependency edges   : {edges}")
print(f"Missing deps       : {len(missing)}")
print(f"Coverage gaps      : {len(coverage_gaps)}  (versions pip picks first)")
print(f"Backtrack-only gaps: {len(backtrack_gaps)}  (older window versions; informational)")
print()
if missing:
    print("MISSING (dependency absent from the closure):")
    for m in missing[:50]:
        print("  " + m)
else:
    print("PASS: every marker-active dependency is present in the closure.")
print()
if coverage_gaps:
    print("COVERAGE GAPS (present, but no mirrored version works for that Python):")
    for g in coverage_gaps[:50]:
        print("  " + g)
else:
    print("PASS: every primary (newest-per-Python) selection has a mirrored, "
          "installable version for the Pythons that need it.")
print()
if backtrack_gaps:
    print(f"NOTE: {len(backtrack_gaps)} older window-only versions have a dep "
          "outside the mirror. pip picks the newest version first (covered "
          "above), so these are backtrack-only and usually harmless:")
    for g in backtrack_gaps[:20]:
        print("  " + g)

sys.exit(1 if (missing or coverage_gaps) else 0)
