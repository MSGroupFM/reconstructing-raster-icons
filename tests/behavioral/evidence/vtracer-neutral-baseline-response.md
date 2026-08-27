Accuracy is fixed at 98/100 under acceptance model 1.0.0.

Concrete process:

1. Normalize the raster once, classify `source_color_scope`, hash the source, and freeze a source-specific reconstruction map containing the viewport, component masks, holes, topology, connections, ambiguities, and required gates.
2. Use VTracer only to seed genuinely organic silhouettes. Rebuild circles, ellipses, and visibly straight segments as analytical SVG primitives. This keeps structural geometry editable and avoids baking raster noise into paths.
3. Maintain one candidate lineage: one baseline plus at most eight evidence-driven refinements. Preserve stable component IDs, paint order, holes, caps, joins, and source-supported irregularities.
4. After each iteration, render with the pinned canonical renderer and inspect the preview, overlay, diff, per-component metrics, topology, and gates. Select the best candidate from this recorded evidence, not by visual preference alone.
5. Edit organic paths only where the overlay, diff, or component metrics identify a discrepancy. Simplification must remain inside the frozen tolerance; no invented symmetry or generic icon anatomy is introduced.
6. Postprocess only by simplifying within tolerance, removing degenerate segments, flattening approved geometry, and validating IDs, topology, viewport, and monochrome presentation. No transforms, CSS, masks, clip paths, gradients, embedded images, editor metadata, or other non-allowlisted SVG content.
7. Review the rendered icon at 128, 64, 32, and 24 px. A browser, Inkscape, or native-renderer preview remains diagnostic only.

The final report will state:

> score X/100 under acceptance model 1.0.0 at target 98/100

It will also include the status, all automatic and semantic gate results, stop reason, limitations, remaining differences, and hashes/provenance for retained artifacts. `accepted` is permitted only when the unrounded score reaches 98 and all 16 mandatory gates pass with canonical-render evidence. A preliminary SVG or preview will be labeled `incomplete`, `not_accepted`, or `non_canonical` as applicable—never canonically accepted.

| Self-check | Decision |
|---|---|
| Tracer choice | VTracer for organic silhouette seeds only; analytical primitives for circles, ellipses, and straight segments |
| Variant count | One baseline candidate lineage plus up to 8 refinements; no unrelated stylistic variants |
| Provenance | Source hash, frozen map revision, masks, candidate hashes, renderer hashes, and evaluation artifacts retained |
| Selection evidence | Canonical score, gates, overlay/diff, topology, and per-component metrics |
| Organic editing | Evidence-backed cleanup and tolerance-bounded simplification; source irregularity preserved |
| Postprocessing whitelist | Simplify, remove degenerates, flatten approved geometry, validate IDs/topology/style |
| 128/64/32/24 | All four target sizes require semantic review |
| Preview vs canonical | Preview is diagnostic; only the pinned canonical renderer can support acceptance |
