# Changelog

All notable changes are recorded here. The format follows Keep a Changelog,
and this project uses semantic versioning.

## Unreleased

- No unreleased changes recorded.

## 0.1.0 — prepared 2026-08-26

This version is prepared locally and has not been tagged, pushed, or
published.

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

- Live Linux-x64 canonical execution and online `npm ci` remain explicitly
  unverified until a real networked Linux CI run completes. The committed CI
  definition does not itself count as execution evidence.
