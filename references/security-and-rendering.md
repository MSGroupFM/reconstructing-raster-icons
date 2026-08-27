# Security and canonical rendering

## Raster input

Accept a single-frame PNG, JPEG, or WebP up to `50 MiB`, `16 MP`, and `8192 px` per side. Treat decoder failure, animation, decompression-bomb warning, low luminance separation, or invalid normalization as `invalid_input`. Apply EXIF orientation, convert embedded ICC to sRGB, preserve alpha, and normalize once with the frozen viewport transform.

## Safe SVG subset

Treat candidates, including every VTracer output, as untrusted bytes. Reject above `5 MiB`, then raw-scan before parsing for NUL, DTD/entity declarations, forbidden URI schemes, and processing instructions other than one leading XML declaration. Parse with `defusedxml.ElementTree`, then allowlist.

Allowed elements: `svg`, `g`, `path`, `rect`, `circle`, `ellipse`, `line`, `polyline`, `polygon`, `title`, `desc`. Permit only published geometry, presentation, ID, role, and accessibility attributes. Final candidates contain no transforms.

Reject scripts, handlers, CSS/style/imports, `foreignObject`, `image`, `text`, `use`, URLs, fonts, animation, filters, gradients, masks, clip paths, editor metadata, and undeclared elements/attributes. Enforce:

- at most `10,000` XML elements;
- at most `2,000,000` path/points characters;
- XML depth at most `64`;
- renderer timeout `15 s`;
- memory limit `512 MiB`.

A security or allowlist failure stops before rendering and yields `invalid_input`; do not repair and continue silently.

## Canonical renderer

Acceptance model `1.0.1` requires the exact `canonical-renderer.lock`:

- Node.js `22.14.0`;
- `@resvg/resvg-wasm@2.6.2`;
- npm integrity `sha512-FqALmHI8D4o6lk/LRWDnhw95z5eO+eAa6ORjVg09YRR7BkcM6oPHU9uyC0gtQG5vpFLvgpeU4+zEAz2H8APHNw==`;
- WASM SHA-256 `22bf6e9f9a100d972da0411a69c5ba504367fc1fa87b3b64e3f35e53926d2d70`;
- loader SHA-256 `10170d02d816f02ec76f9bc095b01d9becf536e7b1e12e5aa616652c84b237a1`.

Use a transparent sRGB canvas; resolve `currentColor` to black; disable system fonts; render explicit dimensions with maximum side `1024`, no background or crop, and pinned shape/text rendering options. Browser, Inkscape, and native librsvg output is preview-only.

Run the Node subprocess with read access limited to the candidate, pinned WASM/loader, runner, and unpredictable owner-only workspace. Node `22.14.0` restricts filesystem, child-process, and worker access through its Permission Model, but that version does not mediate network access. The hash-pinned runner imports no network module and initializes the pinned loader from already-read WASM bytes; the safe SVG subset rejects links and external resources before render. Canonical evidence must not claim runtime network denial. An OS network sandbox may add defense in depth, but it is not part of the cross-platform acceptance contract.

If hashes, Node version, permission model, file allowlist, timeout, or memory isolation cannot be proven, stop before render or report `non_canonical`; never label preview pixels accepted. See the official [Node.js 22.14 Permission Model documentation](https://nodejs.org/download/release/v22.14.0/docs/api/permissions.html) for the exact scopes available in the pinned runtime.

## Artifacts and failures

Keep the workspace through semantic finalization. Reports refer to logical IDs and hashes, not live absolute paths, and record each artifact as `retained|deleted`. After finalization, remove diagnostics unless explicitly requested. For early `invalid_input|runtime_error`, write only a minimal failure report when safe; do not fabricate candidate or metric artifacts.
