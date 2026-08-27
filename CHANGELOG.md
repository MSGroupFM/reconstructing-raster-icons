# Changelog

All notable changes are recorded here. The format follows Keep a Changelog,
and this project uses semantic versioning.

## Unreleased

### Changed

- VTracer is now the mandatory candidate generator after the source map is
  frozen. The workflow requires a recorded multi-variant black-and-white spline
  sweep, evidence-based selection, and same-source trace provenance.
- Shape-changing postprocessing is limited to source-evidenced circles,
  ellipses, and straight segments; manual organic-path reconstruction and
  alternate-tracer fallbacks are prohibited.
- Reports now explicitly distinguish a preview score from canonical acceptance
  and require target-size review at 128, 64, 32, and 24 px.
- Acceptance model `1.0.3` updates canonical renderer attestation to match the actual Node `22.14.0`
  Permission Model. The runner no longer performs an unsupported network probe,
  and conformance failures report non-canonical evidence before checking PNG
  artifacts.
- Canonical resource controls are separated from render semantics: a `15 s`
  parent wall timeout, `512 MiB` V8 old-space limit, and child-attested
  `--disable-wasm-trap-handler`. The contract no longer claims a hard RSS,
  total-memory, or virtual-address-space ceiling; external platform confinement
  remains optional defense in depth.
- The previous Linux `RLIMIT_AS=512 MiB` control bounded the process's total
  virtual address space, so Node `22.14.0` terminated before JavaScript while
  reserving its V8 CodeRange. Disabling the WASM trap handler only avoids a
  separate WebAssembly virtual-address cage, while `--max-old-space-size=512`
  limits V8 old-space alone. Darwin `RLIMIT_DATA` and `RLIMIT_RSS` do not supply
  an equivalent portable hard cap, so none of these OS rlimits is acceptance
  authority in model `1.0.3`.
- Production component diagnostics now extract the selected white component
  with alpha plus relative luminance and recolor explicit fill/stroke throughout
  component subtrees, so black occluders do not contaminate layout, topology,
  or mandatory-component gates.
- GitHub Actions now exposes the repository's `src` layout explicitly instead
  of relying on test import order.
- README now documents the complete workflow and includes real phone and
  strategy-board previews from before and after the VTracer-only process.

## 0.1.0 — local release candidate prepared 2026-08-26

This release candidate is prepared locally and has not been tagged, pushed,
or published. Publication readiness is blocked pending the mandatory live
Ubuntu x64 and macOS 15 arm64 canonical zero-skip CI runs.

### Added

- Accuracy confirmation before any reconstruction write, with `98/100` as the
  proposed default on composite acceptance model `1.0.3`.
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
- Native local Darwin arm64 canonical execution is **VERIFIED** on 2026-08-27:
  all nine cases passed twice with zero skips. The post-push macOS 15 arm64 CI
  record remains **UNVERIFIED**, and Linux x64 GREEN remains **UNVERIFIED**.
  Both live CI records are mandatory publication blockers and must provision
  dependencies independently.
