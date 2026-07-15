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

# allowlist range per package: specifier is everything after the name prefix
allow = {}
for k, v in pkgs.items():
    allow[k] = SpecifierSet(v["allowlist_specifier"])

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

missing = []       # dep not in closure at all
coverage_gaps = [] # in closure, but no mirror version works for the needed Python(s)
edges = 0

for name, info in pkgs.items():
    for vstr in info["versions"]:
        pys = info["per_python"].get(vstr, pyvers)
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
            rng = allow.get(dkey, SpecifierSet())
            rv = real_versions(dkey)
            covered = False
            for v, rp in rv.items():
                if not rng.contains(v, prereleases=True):
                    continue
                if dep.specifier and not dep.specifier.contains(v, prereleases=True):
                    continue
                if any(python_ok(rp, py) for py in pys):
                    covered = True
                    break
            if not covered:
                coverage_gaps.append(
                    f"{name}=={vstr} needs {dep.name}{dep.specifier} on py "
                    f"{','.join(sorted(pys))}; mirror {dkey}{rng} has no usable version")

total_versions = sum(len(i["versions"]) for i in pkgs.values())
print(f"Closure packages   : {len(pkgs)}")
print(f"Selected versions  : {total_versions}  (across Python {', '.join(pyvers)})")
print(f"Dependency edges   : {edges}")
print(f"Missing deps       : {len(missing)}")
print(f"Coverage gaps      : {len(coverage_gaps)}")
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
    print("PASS: every dependency has a mirrored, installable version for the "
          "Pythons that need it.")

sys.exit(1 if (missing or coverage_gaps) else 0)
