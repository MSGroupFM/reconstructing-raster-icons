# VTracer workflow behavioral RED summary

## Neutral baseline

- skill revision: `496f488`
- source: clean `git archive` of that revision under `/private/tmp`;
- prompt: the neutral two-turn scenario in
  `tests/behavioral/scenarios/vtracer-only-pipeline.md`;
- precondition: accuracy already confirmed at `98/100` so the post-intake
  branch is exercised;
- run type: fresh independent agent, no inherited conversation turns, read-only;
- verbatim response: `vtracer-neutral-baseline-response.md`;
- response-file SHA-256:
  `2f292891093e612fa096af8abfa8c3a4e8b6b56794d6d1e02b19ba4ae4458798`;
- result: RED

## Observed gaps

- `SKILL.md` allowed an abstract `build candidate` step and did not require
  VTracer.
- `references/reconstruction-workflow.md` allowed tracing only organic regions
  but did not require multiple VTracer candidates or provenance records.
- “Cleaned organic paths” and simplification allowed manual Bézier redrawing.
- Structural reconstruction allowed a broader set of manual primitives than
  the approved postprocessing whitelist.
- Preview renderers were non-canonical, but the top-level report contract did
  not require a numeric score to be labelled explicitly as a preview score.

## Observed response failures

- VTracer was used only for organic silhouette seeds instead of as the sole
  candidate generator.
- The response proposed one candidate lineage, not at least three same-source
  black-and-white spline variants.
- It explicitly allowed evidence-backed manual editing and simplification of
  organic paths.
- Its postprocessing allowance was broader than the approved shape whitelist.

The baseline did preserve the frozen map, target sizes, and preview/canonical
boundary. It still fails the new process because the four VTracer-generation
and geometry constraints above are mandatory.

## GREEN criteria

The updated skill must require VTracer after the frozen source map, compare a
source-evidenced parameter sweep, retain trace provenance, preserve organic
trace geometry, limit shape postprocessing to circles, ellipses, and evidenced
straight segments, verify 128/64/32/24 px, and distinguish a preview score from
canonical acceptance.
