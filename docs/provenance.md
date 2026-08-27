# Provenance

## Upstream concept review

- Upstream: https://github.com/upbrew-tech/svg-creator-skill
- Reviewed commit: `1eb83d602a992b69a1771c4e0116a9f7de6ffaa2`
- Review date: `2026-08-26`
- Use: concept only

The only retained idea is an iterative workflow: create a candidate, render
it, inspect and compare the result, then revise it. This repository's skill
instructions, Python and Node implementation, JSON Schemas, fixtures, tests,
acceptance model, security controls, and documentation were written anew. No
upstream source code, schema, test, fixture, asset, or documentation text was
copied.

The reviewed upstream commit is recorded to make the conceptual source
auditable and to prevent a moving branch from becoming the provenance record.
The upstream repository is not a runtime dependency.

## Original implementation license

Original material in this repository is provided under Apache License 2.0.
That license does not replace third-party terms. The canonical renderer
dependency `@resvg/resvg-wasm@2.6.2` remains under MPL-2.0 with the exact npm
integrity recorded in `THIRD_PARTY_NOTICES.md`, `package-lock.json`, and
`canonical-renderer.lock`.

The lock's `512 MiB` control is the V8 old-space setting, not a measured or
enforced ceiling for RSS, total process memory, or virtual address space.
Disabling the WASM trap handler changes V8's virtual-address reservation mode;
it does not create a total-memory boundary. External cgroup or platform
confinement is optional defense in depth and is not acceptance provenance.

## Fixture corpus portability

Synthetic fixture corpus `1.0.1` and PNG sub-contract `1.0.0` separate two
reproducibility claims. Two builds on the same host must produce every generated
file with identical raw bytes. A generated corpus compared with the committed
corpus on another supported host treats only PNG IDAT compression payloads and
adjacent IDAT boundaries as non-authoritative. PNG signature, chunk integrity,
IDAT contiguity and position, every ordered non-IDAT chunk, Pillow
format/mode/bands, dimensions, frame count, metadata, and decoded RGBA bytes
remain exact. The joined IDAT payload must be one bounded, complete zlib stream
with no trailing bytes, unconsumed input, or second stream. Its decompressed
length must exactly match the strict IHDR-derived filtered-scanline length,
including Adam7 pass geometry; the filtered bytes themselves are not compared
across platforms. A versioned
`generated_files` inventory is authoritative: each generated tree's actual file
set must equal it, and every declared path must exist in the committed subset.

Draft `source_sha256` values remain relational evidence: each generated and
committed tree independently verifies the field against its own raw sibling
`source.png` before duplicate-rejecting, type-aware decoded JSON comparison.
Only that one verified field is normalized. This cross-platform fixture rule
does not relax release artifact reproducibility; two builds of the release ZIP
must still be byte-identical.

## Publication status

GitHub Actions at `cae7f72` is **VERIFIED** with successful canonical Linux x64
and Darwin arm64 jobs. Corpus `1.0.1` and its portable fixture comparator are
new unpushed working-tree changes, so publication readiness is again blocked
until a post-push full CI run succeeds for the resulting commit. This record
does not assert that a release tag, package, or public release has been created.
