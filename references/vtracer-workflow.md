# Mandatory VTracer workflow

VTracer is the only candidate generator in this reconstruction workflow. The
acceptance renderer remains the separately locked renderer described in
`security-and-rendering.md`; VTracer output is candidate geometry, not
acceptance evidence by itself.

## Preconditions and availability

Do not inspect, install, or run VTracer before the accuracy confirmation
required by `SKILL.md`. Trace only after the reconstruction map, normalized
source, reference masks, and hashes are frozen.

Check `vtracer --version` and `vtracer --help`. The installed CLI must support
black-and-white clustering or the `bw` preset, `spline` mode, threshold or
adaptive thresholding, simplification, speckle filtering, and path precision.
Record the exact version output. If the binary is missing or incompatible,
stop and ask for authorization to install an official VisionCortex VTracer
build. There is no manual fallback, Potrace fallback, generic tracer fallback,
or hand-built candidate.

Treat the VTracer process and every emitted SVG as untrusted. Run it without
unnecessary network access, bound its input/output workspace, and pass every
candidate through the safe-subset checks before rendering.

## Frozen trace input

Every variant must use the same frozen source bytes and normalization revision.
Record at least:

- logical source artifact ID and source hash;
- reconstruction-map revision and hash;
- foreground/background and thresholding decision;
- VTracer version, exact command, and complete parameters;
- output candidate hash.

If crop, orientation, luminance normalization, or foreground extraction is
needed, freeze it before tracing. Never alter the trace input, reference masks,
uncertainty, or tolerances after seeing a candidate. A new input decision
requires a new reconstruction-map revision and a fresh sweep.

## Source-evidenced sweep

Generate at least three black-and-white spline variants. Choose a compact
parameter range from the frozen raster evidence rather than from a previous
icon. The sweep must vary the settings that plausibly explain the remaining
raster uncertainty:

- fixed threshold around the evidenced foreground/background separation, or
  adaptive threshold settings when uneven lighting is documented;
- curve simplification around the smallest value that removes raster noise
  without changing topology;
- speckle filtering only below the smallest semantic component area;
- sufficient path precision to avoid visible coordinate quantization.

Use `bw`/binary clustering and `spline` fitting. Do not introduce color layers
or optimize away semantic components. Keep an immutable variant log containing
the exact command and hashes. A single VTracer result is never enough for
selection, even when its preview looks plausible.

## Selection

Render each safe candidate with the same comparison setup. Compare the full
candidate using the composite metric and inspect preview, overlay, and diff.
Select the trace with the best evidence, not necessarily the fewest nodes.

A semantic component may be copied from another variant only when all of the
following are true:

- both variants trace the same frozen source and normalization revision;
- the component retains its frozen identity, holes, connections, and paint
  order;
- per-component metrics and overlay/diff show a material improvement;
- the substitution copies VTracer geometry without manual node editing;
- the final provenance log maps the component to its candidate hash.

Do not mix geometry from another source, a category template, a manual sketch,
or another tracer.

## Allowed postprocessing

Shape-changing postprocessing is a strict whitelist:

- replace an evidenced round contour with an analytic `circle`;
- replace an evidenced oval contour with an analytic `ellipse`;
- replace a visibly straight contour section with an evidence-backed straight segment.

A closed straight-sided contour may use straight path segments only when every
side and corner is evidenced by the source. Do not infer symmetry, right
angles, equal lengths, or tangency merely because they look cleaner. Do not use
a manual rectangle or polygon as a shortcut for an uncertain traced contour.

Do not redraw, sculpt, or reinterpret organic geometry by hand. That includes
moving organic nodes, replacing an organic VTracer path with a hand-authored
Bézier, smoothing source-supported irregularity, or rebuilding a recognizable
object from generic anatomy. When organic fidelity is weak, select or generate
another VTracer variant within the frozen sweep contract.

Non-shaping cleanup may:

- normalize to the confirmed `viewBox` with centered `contain` by default;
- flatten transforms while preserving the rendered geometry;
- use one opaque foreground and `currentColor` by default;
- restore frozen semantic top-level groups and IDs;
- remove metadata, unsafe elements, redundant groups, and degenerate segments;
- preserve holes, paint order, caps, joins, and editable paths.

## Evaluation and reporting

Evaluate the selected/postprocessed candidate with the frozen map. Inspect
component metrics, topology, preview, overlay, and diff, then review 128, 64, 32, and 24 px. Do not delete legitimate detail merely to pass a small-size
gate; report the failed size and reason.

Retain the VTracer version, variant log, exact commands, source hash, candidate
hashes, selected candidate, component substitutions, and postprocessing log as
reconstruction evidence.

If the locked renderer and its isolation are proven, report the canonical
score and status from acceptance model `1.0.1`. If evaluation uses preview-only
pixels, label the number exactly as a `preview score`, report status
`non_canonical`, and never say `accepted` even when the preview score reaches
the target.
