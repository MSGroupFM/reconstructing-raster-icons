---
name: reconstructing-raster-icons
description: Use when a raster icon or pictogram must become an editable monochrome SVG, or when traced SVG geometry needs measurable fidelity review.
---

# Reconstructing Raster Icons

Reconstruct the evidence in this source, not a remembered icon category. The deliverable is an editable monochrome SVG plus reproducible acceptance evidence.

## First response

Never silently accept an accuracy value and never start work before asking the user to confirm it.

The first user-facing action is this single question, before **any file write or tool call**, even when the request already supplies a value:

> What required accuracy should I set? The default is 98%. In the report this is a 98/100 threshold under a composite model, not 98% identical pixels. Confirm 98% or give another value.

Accept `>0..100` with at most two decimal places. Do not round or lower it.

## Boundary

This version accepts one opaque foreground (`currentColor` by default, or one fixed color) on a transparent background. If colors are semantically distinct, or the source uses gradients or intentional translucency, stop and ask whether to merge them into one silhouette. Treat photo-like input the same way before stylizing it.

## Workflow

1. After confirmation, inventory the source and draft a new reconstruction map: normalization, viewport, components, topology, semantic connections, constraints, ambiguities, gates, and target sizes. Freeze the map and reference masks before generating a candidate. Never reuse source-independent shape rules.
2. Default to `1:1`, grid `64`, centered `contain`; accept ratios from `1:16` through `16:1`, including `16:9`. Use analytical primitives for structural geometry and cleaned paths for organic regions.
3. Run the loop: build candidate → render → view preview/overlay/diff → evaluate → fix evidence-backed differences. Preserve component IDs, holes, paint order, strokes, and editable geometry.
4. Allow a baseline plus `8` refinements by default; a user may set `1..20` refinements. Stop early when best-score gain across three refinements is `<0.10` and no mandatory gate becomes `pass` in those refinements.
5. Never change the target, tolerances, frozen map, or uncertainty to obtain acceptance. At stall or limit return the best candidate as `not_accepted` or `incomplete`, with remaining differences.

Report: “score X/100 under acceptance model 1.0.0 at target Y/100,” plus status, gates, stop reason, limitations, and retained artifacts. Never say only “X% accurate.”

## Quick reference

| Need | Read |
|---|---|
| Intake, map, reconstruction, CLIs | [Workflow](references/reconstruction-workflow.md) |
| Scores, gates, statuses, stopping | [Acceptance model](references/acceptance-model.md) |
| Safe SVG, limits, canonical renderer | [Security and rendering](references/security-and-rendering.md) |

## Common mistakes

- Starting because the user said “no questions,” or silently taking `98`.
- Inventing anatomy, balance, or grid rules not evidenced by this source.
- Merging meaningful colors without confirmation.
- Relabeling a failed target “conditionally ready” or changing its tolerance.
