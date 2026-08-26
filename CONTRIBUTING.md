# Contributing

Contributions must preserve the deterministic acceptance contract, immutable
artifact lifecycle, fail-closed renderer boundary, and monochrome scope of
`0.1.x`.

## Development setup

Use Python 3.11–3.13 and the exact dependency graphs:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-lock.txt
npm ci --no-audit --no-fund
```

Do not install packages during a reconstruction run. Do not replace the pinned
renderer with a browser, native renderer, or system Node fallback for
acceptance evidence.

## RED/GREEN workflow

For a feature or bug fix:

1. Write the smallest test that exercises the real user-visible or archive
   behavior.
2. Run that focused test and record the expected RED failure.
3. Implement only enough production code to pass it.
4. Rerun the focused test, then the complete suite.
5. Refactor only while tests remain green.

Derive expected metric values independently. Do not copy production output
into a golden, assert against a mock as if it were the renderer, or fabricate
canonical authority on an unsupported platform.

## Checks

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python -m py_compile src/reconstructing_raster_icons/*.py scripts/*.py
python scripts/validate_schemas.py --schemas schemas \
  --documents tests/fixtures/contracts/valid-*.json
python scripts/validate_skill.py --path .
python scripts/build_release.py \
  --source . --output /path/to/release-output --version 0.1.0
git diff --check
```

The system `quick_validate.py` is an optional local Codex check and is not a
portable CI dependency. On supported Linux x64, the canonical-platform
conformance class must run with zero skips. On unsupported hosts, preserve the
explicit `non_canonical` or documented skip; never substitute oracle evidence.

## Fixtures and reports

- Synthetic fixtures must be original and reproducible from
  `tests/fixtures/build_fixtures.py`.
- Do not commit run workspaces, preview/overlay/diff diagnostics, caches,
  release ZIPs, or checksums.
- Do not add absolute workstation paths, credentials, remote URLs that imply a
  publication, or unverifiable compatibility claims.
- Keep logical artifact IDs and SHA-256 values in machine reports; do not
  preserve stale live paths after finalization.

## Dependency updates

Renderer changes require a new acceptance-model version and regenerated
goldens. To update only the Python lock, use a clean Python 3.11 environment,
install exactly `pip-tools==7.5.0`, run:

```bash
python -m piptools compile --generate-hashes \
  requirements.txt --output-file requirements-lock.txt
python -m pip install --require-hashes -r requirements-lock.txt
```

Record the Python, pip, and pip-tools versions used. Review lock and license
changes before committing.
