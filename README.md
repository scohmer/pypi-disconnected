# PyPI-disconnected

Build a self-hosted, **offline** PyPI `/simple/` repository from a project's
`requirements.txt`. Point it at a local file or a GitHub repo; it resolves the
full transitive dependency closure (from each listed version *forward*, uncapped by
default), mirrors
the wheels and PEP 503 metadata for your target platforms, Python versions, and
CPU architectures, verifies that nothing is missing, and produces a directory
you can copy to an air-gapped host and serve.

## Push-button workflow

Everything is driven by **one file**: `config/settings.toml`.

```bash
# 1. CONNECTED build machine
pip install -r requirements-tooling.txt     # bandersnatch, packaging, ...
$EDITOR config/settings.toml                 # set the source + targets (only edit here)
scripts/build_mirror.sh                      # resolve -> closure-check -> config -> mirror
scripts/verify_mirror.sh                     # prove `pip install -r ...` works offline

# 2. copy ./mirror to the DISCONNECTED host, then there:
scripts/serve_mirror.sh                      # reads settings.toml for mode/port
```

Clients on the air-gapped network:

```bash
pip install --index-url http://<host>:8080/simple/ --trusted-host <host> -r requirements.txt
# or make it permanent:
pip config set global.index-url http://<host>:8080/simple/
pip config set global.trusted-host <host>
```

## How it works

```
requirements.txt  (local file OR GitHub URL; -r includes followed)
        |
        v
 resolve_deps.py ----------> build/allowlist.txt   (PEP 440 specifiers)
   * cap versions             build/lock.json       (audit / reproducibility)
   * walk transitive deps     build/report.txt      (LOUD: anything missing)
   * evaluate markers across python x OS x ARCH
   * conflicts -> keep newest + widen range (never drop a package)
        |
        v
 check_closure.py  --------> asserts every dependency edge is satisfied
        |
        v
 generate_bandersnatch_conf.py -> build/bandersnatch.conf
        |
        v
 bandersnatch mirror  ------> mirror/web/simple/    (PEP 503 tree)
   (CONNECTED machine)        mirror/web/packages/  (wheels + sdists)
        |
        v   copy mirror/ to the air-gapped host
        v
 serve_mirror.sh  ---------> http://host:8080/simple/
   (DISCONNECTED machine)
```

`resolve_deps.py` needs network access to PyPI (run it on the connected
machine). It only reads metadata; bandersnatch does the file downloads.

## What was fixed (why offline installs now succeed)

The previous version silently left holes in the mirror. The important fixes:

1. **Every target Python resolves independently (3.9-3.13).** A package's newest
   release often drops old-Python support (e.g. newest `numpy` needs >=3.11), so
   on 3.9 pip installs an *older* version with *different* dependencies. The
   resolver selects the latest N versions **compatible with each target Python**
   (`requires_python`-aware) and walks each distinct version's dependency tree,
   so the mirror carries a working version — and its deps — for every
   interpreter. It then runs **coverage repair**: any specific version the
   closure requires but the window missed (e.g. a transitive `==` pin like
   `capstone==5.0.0.post1`) is added as an explicit allowlist pin, so windowing
   never breaks an install. Verified: 0 missing deps and 0 coverage gaps for the
   version pip picks first, across 3.9-3.13.
2. **Allowlist range now contains the resolved version.** The cap used to be
   the next major of the *listed floor*, so `cryptography>=1.4` became
   `>=1.4,<2` — which excluded the version actually resolved (48.x) and every
   version its dependants needed. That mandatory cap has been removed: by
   default the allowlist is uncapped (`>=floor`), and the floor widens **down**
   to the lowest version any dependant requires. So conflict-heavy packages
   (`cryptography`, `wcwidth`, `arpy`, `pillow`, `filelock`,
   `typing-extensions`) include every version some part of the graph asks for.
3. **Complete, architecture-aware marker environments.** Dependency markers are
   evaluated against the full matrix of python x OS x **CPU architecture**
   (`platform_machine` is set explicitly). Previously `platform_machine`-guarded
   deps (e.g. the `nvidia-*-cu12` stack pulled by `torch`) could be dropped
   because evaluation fell back to the build machine's values. Unevaluable
   markers are treated as *true* (mirror a superset rather than risk a hole).
4. **Conflicts never drop a package.** When accumulated constraints are
   mutually unsatisfiable, the resolver keeps the newest release, widens the
   allowlist to cover the conflicting lower bounds, and records it in
   `report.txt` instead of skipping the package.
5. **`-r`/`-c` includes are followed** (recursively, relative to the source).
6. **Build essentials seeded.** `pip`, `setuptools`, `wheel` are always
   included so the disconnected host can bootstrap and build sdists.
7. **A loud report + a real closure check.** `report.txt` lists typos
   (names not on PyPI), unmirrorable lines, conflicts, and sdist-only packages
   with no declared metadata. `check_closure.py` then proves every dependency
   edge is satisfied by the closure.

## Design decisions

**Version coverage — "latest N per Python, then repair."** By default
(`cap_level = "window"`, `window_size = 4`) the mirror keeps, for each package,
the latest `window_size` versions **compatible with each configured Python** —
so every interpreter has recent, installable choices while the mirror stays
small. Coverage repair then adds any extra version the dependency closure needs
but the window missed. Alternatives (optional size/shape levers): `none` mirrors
everything from the listed version forward (largest, simplest); `major`/`minor`
bound that forward range to the resolved version's series.

**Targets are explicit.** Wheels are mirrored only for the platforms, Python
versions, and architectures you declare. Pure-Python (`py3-none-any`) wheels and
sdists are always kept. This is the main lever on mirror size.

**bandersnatch does the mirroring.** It produces a PEP 503 tree with per-file
SHA256 hashes and (with `json = true`) JSON metadata. We only generate its
allowlist and platform filters.

**Reproducible.** `lock.json` records every resolved version, the allowlist
specifier emitted, and which package required it. It is also copied into
`mirror/meta/` so the disconnected side can audit and re-verify.

## Configuration (`config/settings.toml`)

Everything lives here — there is no second place to edit.

- `[source]` — `requirements_file` (local, takes precedence) **or** `github_url`
  (repo / blob / raw). `-r` includes inside the file are followed.
- `[versions]` — `cap_level` (`window` default = latest `window_size` per Python; `none`/`major`/`minor` alternatives), `window_size`, `include_prereleases`.
- `[targets]` — `python_versions` (default `3.9`-`3.13`; each is resolved
  independently), `platforms` (`linux`/`windows`/`macos`/`freebsd`),
  `architectures` (`x86_64`/`arm64`).
- `[resolve]` — `ensure_packages` (default pip/setuptools/wheel), `strict`
  (fail the build if any requirement can't be resolved).
- `[mirror]` — `output_dir`, `master`, `workers`, `keep_json`.
- `[serve]` — `mode` (`static`/`pypiserver`), `port`.

## Verify before you go offline

`scripts/verify_mirror.sh` serves the freshly built mirror on localhost and asks
`pip install --dry-run` to resolve your requirements using **only** that index —
no pypi.org. It excludes names already flagged as missing in `report.txt`.

Note: pip resolves for the interpreter/OS it runs on. Run `verify_mirror.sh` on
a host matching each target (or in per-target containers) for full coverage of
every python/platform combination.

`scripts/check_closure.py` (run automatically inside `build_mirror.sh`) does a
build-free, **per-Python** check: for every package, for every version selected
across Python 3.9-3.13, it confirms each marker-active dependency is present in
the closure AND that the mirror actually contains a version installable on the
Pythons that need it. It exits non-zero on any missing dependency or coverage
gap.

## Running pieces individually

```bash
# Resolve only:
python3 scripts/resolve_deps.py \
    --requirements-file input/requirements.txt \
    --python-versions 3.9 3.10 3.11 3.12 \
    --platforms linux windows --architectures x86_64 \
    --cap-level major --strict \
    --out-allowlist build/allowlist.txt \
    --out-lock build/lock.json --out-report build/report.txt

# (or --github-url https://github.com/owner/repo)

# Check the closure:
python3 scripts/check_closure.py build/lock.json build/.metacache

# Generate config from an allowlist:
python3 scripts/generate_bandersnatch_conf.py \
    --allowlist build/allowlist.txt --output-dir ./mirror \
    --platforms linux windows --python-versions 3.9 3.10 3.11 3.12
```

## Layout

```
config/settings.toml                  the ONLY file you edit
input/requirements.txt                default local source (or point at GitHub)
scripts/resolve_deps.py               source -> dependency closure -> allowlist + lock + report
scripts/check_closure.py              proves the closure is complete (no missing deps)
scripts/generate_bandersnatch_conf.py allowlist + targets -> bandersnatch.conf
scripts/build_mirror.sh               orchestrates the full build (connected)
scripts/verify_mirror.sh              simulates the offline install (connected)
scripts/serve_mirror.sh               serves the mirror (disconnected)
requirements-tooling.txt              build-machine dependencies
build/                                generated artifacts (gitignored)
mirror/                               the mirror output, incl. meta/ (gitignored)
```

## Limitations & notes

- `resolve_deps.py` needs network access to PyPI; run it on the connected machine.
- **sdist-only packages with no declared metadata** (listed in `report.txt`) can
  hide runtime dependencies in `setup.py`. pip must *build* them to learn their
  deps, which needs their system build prerequisites on the target
  (e.g. `mysqlclient` needs libmysqlclient headers, `mod_wsgi` needs Apache
  headers). Review that list.
- **Genuine conflicts in your requirements are reported, not hidden.** If two
  packages demand incompatible versions of the same dependency (e.g.
  `termscraper` needs `wcwidth==0.2.5` while `prettytable` needs `>=0.3.5`),
  no single `pip install -r` can satisfy both — online or offline. The mirror
  still contains every needed version so any resolvable subset installs.
- Verify `bandersnatch` filter plugin names against your installed version
  (`allowlist_project`, `allowlist_release`, `exclude_platform` are current as
  of 6.x/7.x).
