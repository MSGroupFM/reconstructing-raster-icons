# Security policy

## Scope and threat model

Raster sources, reconstruction drafts, semantic reviews, and candidate SVGs
are untrusted input. Relevant threats include XML entities and processing
instructions, scripts and external resources, unsafe URI schemes, filesystem
escape, decompression bombs, oversized geometry, renderer exhaustion,
dependency substitution, forged canonical evidence, and archive traversal.

The implementation therefore:

- pre-scans SVG bytes and parses with `defusedxml` before applying a strict
  element and attribute allowlist;
- rejects network/data resources, scripts, fonts, filters, animation, raster
  embedding, transforms, and unsupported XML features;
- bounds raster/SVG size, image dimensions, element count, path data, nesting,
  parent wall time, and V8 old-space. The old-space setting is not a hard RSS,
  total-memory, or virtual-address-space ceiling;
- verifies Node, package, loader, WASM, and runner identities against
  `canonical-renderer.lock`;
- uses the Node `22.14.0` Permission Model for filesystem, child-process, and
  worker restrictions without claiming a network permission that this runtime
  does not implement; the pinned runner has no network import, initializes the
  loader from already-read WASM bytes, and rejects external SVG resources;
- treats those Permission Model checks as seat belts around trusted,
  hash-pinned Node and runner code, not as a malicious-code sandbox or a
  containment boundary for a compromised dependency;
- fails closed as `non_canonical` if the exact runtime, Permission Model
  capabilities, child-observed V8 flags, or parent timeout cannot be proven;
- writes immutable stage artifacts and uses logical IDs plus SHA-256 evidence;
- builds releases without symlinks or local paths and validates traversal-safe
  extraction before reporting success.

The precise limits and accepted SVG subset are documented in
`references/security-and-rendering.md`.

## Supported line

Security fixes are prepared for the `0.2.x` line. Version `0.2.0` is currently
under local development. Publication readiness remains blocked pending the
mandatory live Ubuntu x64 and macOS 15 arm64 canonical zero-skip CI runs.
Native local Darwin arm64 canonical execution is **VERIFIED** for the `0.2.0`
worktree on 2026-09-01 under CPython `3.14.7`: all nine cases passed twice.
The post-push macOS CI record remains **UNVERIFIED**, and the `0.2.0` Linux x64
GREEN remains **UNVERIFIED**. This is not a claim that a public release or
support service exists.

## Reporting a vulnerability

Do not publish exploit details, malicious fixtures, or suspected vulnerabilities
in a public issue. This source tree has no configured public security contact
and no remote repository. Report privately to the repository owner or
maintainer through the private channel by which you received the source. A
future public release must designate a private security contact before
publication.

Include the affected commit/version, platform, minimal reproduction, impact,
and whether the input crossed the parser, renderer, pipeline, or archive
boundary. Remove credentials and personal data. Maintainers should acknowledge
the report privately, reproduce it with a failing regression, prepare a fix,
and coordinate disclosure only after affected users can update.

## Dependency reports

For a third-party vulnerability, identify the exact locked package and
integrity where possible. Do not solve a renderer advisory by silently changing
WASM bytes: renderer changes require a new acceptance-model version, reviewed
locks, and regenerated canonical goldens.
