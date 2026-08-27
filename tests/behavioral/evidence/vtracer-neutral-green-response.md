Confirmed. The required threshold is locked at 98/100 under acceptance model 1.0.0. It does not mean 98% identical pixels.

Concrete process:

1. Normalize the monochrome source once, then freeze its hash, foreground/background decision, `source_color_scope: monochrome`, reference masks, reconstruction map, and viewport. Unless the source requires an evidenced alternative, the defaults are a centered `contain` fit, transparent padding, `currentColor`, and a `64 × 64` viewBox.
2. Inventory every semantic component, including organic silhouettes, circles, ellipses, straight sections, holes, connections, overlaps, and paint order. Ambiguities and mandatory gates are frozen before candidate generation.
3. Generate at least three black-and-white spline candidates with VTracer from the exact same frozen source. Record the VTracer version, complete command and parameters, source/map hashes, and every candidate hash.
4. Compare complete candidates using the composite metrics, component metrics, preview, overlay, and diff. Component substitution is permitted only between those same-source candidates and only when measurable component-level evidence supports it.
5. Preserve VTracer-generated organic paths without manual node editing. Shape-changing postprocessing is limited to source-evidenced analytic circles, ellipses, and visibly straight segments. Non-shaping cleanup may normalize the viewBox, flatten transforms without changing geometry, apply `currentColor`, restore semantic IDs/groups, remove unsafe or redundant content, and preserve holes and layer order.
6. Evaluate the baseline and up to eight refinements. Review the icon at 128, 64, 32, and 24 px without deleting legitimate details merely to improve a small-size rendering.
7. Finalize only after all automatic and semantic gates have evidence. The report will include the composite score, gate results, stop reason, limitations, complete trace provenance, postprocessing log, and retained-artifact hashes.

No preliminary rendering or VTracer output will be described as canonically accepted. If the locked renderer and its isolation are proven, the final wording will be:

> score X/100 under acceptance model 1.0.0 at target 98/100

Otherwise it will be reported as:

> preview score X/100 — status: non_canonical

A score cannot override a failed gate. If refinement stalls or reaches its limit below the required result, the best candidate will be returned as `not_accepted` or `incomplete`, with the remaining differences listed. No final score is asserted before evaluation evidence exists.

| Self-check | Decision |
|---|---|
| Tracer choice | VTracer only; no manual or substitute tracer |
| Variant count | At least three same-source B/W spline variants |
| Provenance | Version, exact commands, parameters, source/map/candidate hashes |
| Selection evidence | Composite and per-component metrics, preview, overlay, diff |
| Organic editing | No manual redrawing, sculpting, smoothing, or node movement |
| Postprocessing whitelist | Evidenced circles, ellipses, and straight segments; non-shaping cleanup only otherwise |
| 128/64/32/24 | All four sizes reviewed and reported |
| Preview vs canonical | Preview remains `non_canonical`; acceptance requires the locked renderer and all gates |
