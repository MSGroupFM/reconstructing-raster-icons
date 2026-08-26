# Changelog

All notable changes are recorded here. The format follows Keep a Changelog,
and this project uses semantic versioning.

## Unreleased

- No unreleased changes recorded.

## 0.1.0 — local release candidate prepared 2026-08-26

This release candidate is prepared locally and has not been tagged, pushed,
or published. Publication readiness is blocked pending the mandatory live
Ubuntu x64 canonical zero-skip CI run.

### Added

- Accuracy confirmation before any reconstruction write, with `98/100` as the
  proposed default on composite acceptance model `1.0.0`.
- Monochrome reconstruction workflow with frozen maps, reference masks,
  component inventory, analytical/organic geometry guidance, eight-refinement
  limit, and stall detection.
- Safe raster normalization and allowlisted SVG parsing with bounded resource
  use.
- Hash-pinned Node `22.14.0` and `@resvg/resvg-wasm@2.6.2` rendering contract.
- Composite `S/C/L/T` metrics, nine automatic gates, seven semantic gates,
  deterministic status precedence, and immutable stage/final reports.
- Synthetic conformance, adversarial, schema, pipeline, renderer, and
  behavioral test coverage.
- Repository-local skill/schema validators, SHA-pinned CI definition, and a
  deterministic traversal-safe release builder.

### Security

- Candidate SVG is rejected before renderer invocation when raw XML or the
  safe-subset contract fails.
- Release archives omit caches, symlinks, raw behavioral evidence, run
  diagnostics, installed dependencies, and machine-local path leaks.

### Verification boundary

- A clean Python `3.11.16` environment installed `requirements-lock.txt` with
  `--require-hashes`, and online host `npm ci --no-audit --no-fund` completed
  with the locked Node `22.14.0` and WASM artifacts verified.
- Live Ubuntu Linux-x64 canonical execution remains explicitly **UNVERIFIED**
  and mandatory with zero skips before publication readiness. Its CI job must
  run online `npm ci` independently; a host installation and the committed CI
  definition do not count as Linux execution evidence.
