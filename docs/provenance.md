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

## Publication status

This provenance record accompanies a local release candidate whose publication
readiness is blocked pending mandatory live Ubuntu x64 and macOS 15 arm64
canonical zero-skip CI.
Native local Darwin arm64 canonical execution passed all nine cases twice on
2026-08-27. That host evidence is **VERIFIED**, but the post-push macOS CI record
and Linux x64 GREEN remain **UNVERIFIED** and are both publication blockers.
It does not assert that a GitHub repository, remote, tag, package, or release
has been created or published.
