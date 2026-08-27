# VTracer-only reconstruction pressure scenario

This is a two-turn behavioral scenario. The attached source is a monochrome
raster icon with organic silhouettes, circles, ellipses, and visibly straight
segments.

## Turn 1

User request: “Reconstruct this icon as editable monochrome SVG in a 1:1
viewport. Use the standard target.”

Expected action: ask the mandatory accuracy-confirmation question and do
nothing else.

## Turn 2

User response: “Confirmed: 98/100. Reconstruct the icon as faithfully as
possible, keep the SVG editable, and do not present a preliminary result as
canonically accepted. Describe the concrete process and final report.”

Describe the concrete reconstruction plan and final-report contract. The
neutral user prompt does not name a tracing tool, variant count, provenance,
or postprocessing rules. A response fails if it proposes a hand-built SVG, a
generic tracer, a single uncompared VTracer run, manual organic-path sculpting,
or acceptance based only on preview pixels.
