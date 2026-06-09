# PyPI-disconnected

Build a self-hosted, **offline** PyPI `/simple/` repository from a project's
`requirements.txt`. Point it at a GitHub repo; it resolves the full transitive
dependency closure (from each listed version *forward*, capped within the same
major series), mirrors the wheels and PEP 503 metadata for your target
platforms and Python versions, and produces a directory you can copy to an
air-gapped host and serve.

## How it works

```
GitHub requirements.txt
        │
        ▼
 resolve_deps.py ──────────► allowlist.txt  (PEP 440 specifiers)
   • fetch requirements                       + lock.json (audit/reproducibility)
   • cap versions (>=listed, <next major)
   • walk transitive deps via PyPI JSON API
   • evaluate markers across target matrix
        │
        ▼
 generate_bandersnatch_conf.py ─► bandersnatch.conf
        │
        ▼
 bandersnatch mirror  ──────────► mirror/web/simple/   (PEP 503 tree)
   (CONNECTED machine)            mirror/web/packages/  (wheels + sdists)
        │
        ▼   copy directory to air-gapped host
        ▼
 serve_mirror.sh  ─────────────► http://host:8080/simple/
   (DISCONNECTED machine)
```

The build runs on a **connected** machine (bandersnatch needs PyPI access). The
output directory is then transferred to the **disconnected** host, where it is
served as static files — no internet required.

## Design decisions

**Version coverage — "forward, capped to the major series."** For each
top-level package the lower bound is the version listed in `requirements.txt`;
the upper bound is the next major version (e.g. `requests==2.28.0` →
`requests>=2.28.0,<3`). Transitive dependencies are capped the same way from
their resolved version. This is configurable (`cap_level = "major"` or
`"minor"` in `settings.toml`). Note for `0.x` packages, a "major" cap is wide —
switch to `minor` if those projects matter to you.

**Targets are explicit.** Wheels are mirrored only for the platforms
(`linux`, `windows`) and Python versions (3.9–3.12) you declare. Pure-Python
(`py3-none-any`) wheels and sdists are always kept. This is the main lever on
mirror size — every extra platform/interpreter multiplies it.

**bandersnatch does the mirroring.** It produces a PEP 503 compliant tree with
per-file SHA256 hashes and (with `json = true`) the JSON metadata, and handles
yanked releases and edge cases. We only generate its allowlist and platform
filters — we don't hand-roll downloading or metadata generation.

**Wheels preferred over sdists.** Wheels install without a build step, which an
air-gapped host can't easily perform (it would need build backends and their
deps too). Packages that ship sdist-only still come through.

**Reproducible.** `lock.json` records every resolved version, the specifier
emitted, and which package required it — so you can diff, audit, and re-run.

## Usage

On the **connected** build machine:

```bash
pip install -r requirements-tooling.txt        # bandersnatch, packaging, ...
$EDITOR config/settings.toml                    # set github_url + targets
scripts/build_mirror.sh                         # resolve → config → mirror
```

This writes `build/allowlist.txt`, `build/lock.json`, `build/bandersnatch.conf`,
and the mirror itself to `mirror/` (per `settings.toml`).

Copy `mirror/` to the **disconnected** host, then:

```bash
scripts/serve_mirror.sh static  ./mirror  8080      # zero-dependency HTTP
# or
scripts/serve_mirror.sh pypiserver ./mirror 8080    # via pypiserver
```

Clients on the air-gapped network install with:

```bash
pip install --index-url http://<host>:8080/simple/ <package>
# or make it permanent:  pip config set global.index-url http://<host>:8080/simple/
#                        pip config set global.trusted-host <host>
```

### Running pieces individually

```bash
# Resolve only (writes allowlist.txt + lock.json):
python3 scripts/resolve_deps.py \
    --github-url https://github.com/owner/repo \
    --python-versions 3.9 3.10 3.11 3.12 \
    --platforms linux windows \
    --cap-level major

# Generate config from an allowlist:
python3 scripts/generate_bandersnatch_conf.py \
    --allowlist allowlist.txt --output-dir ./mirror \
    --platforms linux windows --python-versions 3.9 3.10 3.11 3.12
```

You can also resolve from a local file with `--requirements-file path.txt`.

## Layout

```
config/settings.toml                  central config
scripts/resolve_deps.py               GitHub → dependency closure → allowlist + lock
scripts/generate_bandersnatch_conf.py allowlist + targets → bandersnatch.conf
scripts/build_mirror.sh               orchestrates the full build (connected)
scripts/serve_mirror.sh               serves the mirror (disconnected)
requirements-tooling.txt              build-machine dependencies
build/                                generated artifacts (gitignored)
mirror/                               the mirror output (gitignored)
```

## Limitations & notes

- `resolve_deps.py` needs network access to PyPI; run it on the connected machine.
- Nested `-r other.txt` includes in requirements files are skipped with a warning
  (only the fetched file is parsed). Resolve those separately if needed.
- The resolver picks the newest version satisfying constraints to read
  dependency metadata; if two packages need conflicting majors, both ranges are
  allowlisted (bandersnatch mirrors a superset, which is fine for a mirror — pip
  resolves the exact set at install time on the client).
- `exclude_platform` filters by OS and Python *minor* version tags. Some wheels
  with unusual tags may slip through; this only affects mirror size, not
  correctness.
- Verify the `bandersnatch` filter plugin names against your installed version
  (`allowlist_project`, `allowlist_release`, `exclude_platform` are current as of
  6.x/7.x).
