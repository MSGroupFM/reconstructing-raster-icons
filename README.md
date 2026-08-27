# Reconstructing Raster Icons

`reconstructing-raster-icons` is a Codex skill and deterministic validation
toolkit for rebuilding a raster icon as editable SVG. It records the source
structure before tracing, generates candidates only with VTracer, permits only
source-evidenced circle, ellipse, and straight-segment corrections, renders the
candidate with a hash-pinned WASM renderer, and reports a composite fidelity
score plus blocking automatic and semantic gates.

Version `0.1.0` is prepared for monochrome icons only: one opaque foreground
color on a transparent background. Meaningful multicolor, gradients,
intentional translucency, photographs, and painterly images require a declared
analysis/user scope decision and stop for its required confirmation instead of
being silently flattened. The deterministic CLI enforces the declaration; it
does not claim automatic semantic color detection.

## Requirements

- Python `3.11`, `3.12`, or `3.13`;
- Node.js `22.14.0` for dependency installation;
- the exact npm graph in `package-lock.json`, including
  `@resvg/resvg-wasm@2.6.2` and the platform-pinned Node binary;
- Python packages from `requirements-lock.txt`.
- the official [VisionCortex VTracer](https://www.visioncortex.org/vtracer/)
  CLI with black-and-white spline tracing,
  threshold/adaptive controls, simplification, speckle filtering, and path
  precision support. Record `vtracer --version` and every exact trace command;
  VTracer is a candidate generator, not the canonical acceptance renderer.

The acceptance renderer does not download dependencies while evaluating an
icon. Provision dependencies before starting a reconstruction.

## Install from a source archive

Extract the archive into a dedicated directory, then install the locked
dependencies:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-lock.txt
npm ci --no-audit --no-fund
```

The readable direct dependency ranges remain in `requirements.txt`; use the
hashed lock for reproducible installation. To expose the skill to Codex, place
or link the extracted repository under your personal skills directory using
your local Codex setup. The archive does not assume a machine-specific path.

## Required first question

Before any output write or reconstruction tool call, the skill asks exactly:

> Какую требуемую точность задать? По умолчанию — 98%. В отчёте это будет
> порог 98/100 по составной модели, а не 98% одинаковых пикселей.
> Подтвердите 98% или укажите другое значение.

Even if the request already names a value, it must be confirmed. The value is
a threshold on acceptance model `1.0.2`, not a percentage of identical pixels.

## How the skill works

The skill separates candidate generation from acceptance. VTracer produces
editable geometry; the locked evaluator and semantic review decide whether a
candidate meets the confirmed target.

1. **Confirm accuracy.** The skill asks the required question above before it
   writes a file, inspects or installs VTracer, or starts reconstruction. The
   default is `98/100`, but it is never silently assumed.
2. **Freeze the source contract.** Normalize the source once, decide the
   monochrome foreground/background treatment, inventory components and
   topology, confirm the viewport, and freeze the reconstruction map, masks,
   ambiguities, gates, and hashes.
3. **Generate a VTracer sweep.** Trace the exact same frozen source into at
   least three black-and-white spline candidates. Record the VTracer version,
   every complete command and parameter set, and all source, map, and candidate
   hashes.
4. **Select from evidence.** Compare complete candidates with the composite
   metric, component metrics, preview, overlay, and diff. A component can be
   taken from another candidate only when both candidates trace the same frozen
   source and the component evidence is measurably better.
5. **Apply constrained postprocessing.** Preserve VTracer-generated organic
   paths. Shape-changing corrections are limited to source-evidenced circles,
   ellipses, and visibly straight segments. Non-shaping cleanup may normalize
   the `viewBox`, flatten transforms without changing geometry, apply
   `currentColor`, restore semantic IDs/groups, and remove unsafe or redundant
   content.
6. **Evaluate without moving the goalposts.** Run the baseline plus up to eight
   evidence-driven refinements by default, review `128`, `64`, `32`, and `24`
   px, and never change the target, tolerances, frozen map, or uncertainty to
   obtain acceptance.
7. **Report the real status.** Canonical acceptance requires the locked
   renderer, its isolation, the unrounded composite score, and every mandatory
   gate. Preview-only evaluation is labelled `preview score` with status
   `non_canonical`; a failed or stalled reconstruction remains `not_accepted`
   or `incomplete` with the remaining differences listed.

If VTracer is unavailable or incompatible, the workflow stops and reports the
blocker. It does not silently switch to a hand-built SVG or another tracer.

### Example request

```text
User: Reconstruct this raster icon as editable monochrome SVG in a 1:1
viewport. Use the standard target.

Skill: What required accuracy should I set? The default is 98%. In the report
this is a 98/100 threshold under a composite model, not 98% identical pixels.
Confirm 98% or give another value.

User: Confirmed: 98/100.
```

Only after the confirmation does the skill freeze the source contract and run
the VTracer sweep.

## Behavioral example: before and after the VTracer contract

The repository contains a RED/GREEN pressure test using the same neutral
two-turn request. The control is not an agent with no instructions at all: it
uses the previous skill revision `496f488`, before the current mandatory
VTracer contract. In this comparison, “without the current skill” means
without that contract.

| Decision | Previous revision — without the current contract | Current skill |
|---|---|---|
| Candidate generation | VTracer seeded only organic silhouettes; analytical geometry could be built separately | VTracer is the only candidate generator |
| Candidate search | One baseline lineage plus refinements | At least three same-source B/W spline variants before selection |
| Organic geometry | Evidence-backed manual cleanup and simplification were allowed | Manual redrawing, sculpting, smoothing, and node movement are prohibited |
| Provenance | General source, map, candidate, renderer, and evaluation hashes | VTracer version, exact commands and parameters, plus source, map, and candidate hashes |
| Selection | Refine one lineage from evaluation evidence | Compare complete variants; substitute components only from the same frozen source with per-component evidence |
| Reporting | Already preserved the preview/canonical boundary | Preserves that boundary and explicitly labels preview-only numbers `preview score` / `non_canonical` |

The control response proposed:

> Use VTracer only to seed genuinely organic silhouettes. Rebuild circles,
> ellipses, and visibly straight segments as analytical SVG primitives.

It also allowed organic edits when an overlay, diff, or component metric showed
a discrepancy. The current response instead required:

> Generate at least three black-and-white spline candidates with VTracer from
> the exact same frozen source.

and preserved organic paths without manual node editing. That change made all
eight behavioral criteria pass: mandatory VTracer, variant sweep, provenance,
evidence-based selection, organic-geometry preservation, the strict
postprocessing whitelist, target-size review, and honest reporting.

The reusable [neutral scenario](tests/behavioral/scenarios/vtracer-only-pipeline.md)
is included in the release. Development-tree-only evidence is intentionally
omitted from the ZIP: `tests/behavioral/evidence/vtracer-red-summary.md`,
`vtracer-neutral-baseline-response.md`, `vtracer-green-summary.md`, and
`vtracer-neutral-green-response.md`. The summaries record response hashes so
the quoted passes can be checked in a source checkout without creating broken
links in the packaged README.

## Visual examples from the reconstruction passes

These previews use the actual phone and strategy-board passes that motivated
the VTracer-only workflow. They are visual illustrations, not acceptance
evidence: the composite score, gates, hashes, and canonical renderer report
remain authoritative.

### Vintage phone

| Prior external/manual reconstruction | VTracer workflow plus allowed postprocessing |
|---|---|
| ![Prior phone reconstruction with simplified web and telephone geometry](docs/examples/vintage-phone-before.png) | ![Phone reconstructed with VTracer and constrained primitive corrections](docs/examples/vintage-phone-vtracer.png) |

The prior file used hand-authored structure and a nonconforming rectangular
viewport. The current pass preserves the source-specific web, handset,
telephone body, dial, and spider in a square viewport while keeping analytical
round elements editable.

### Strategy board

| Earlier hand-reconstructed icon | VTracer workflow plus allowed postprocessing |
|---|---|
| ![Earlier manually reconstructed strategy-board icon](docs/examples/strategy-board-before.png) | ![Strategy board reconstructed with VTracer and constrained line corrections](docs/examples/strategy-board-vtracer.png) |

The earlier result regularized the stand and other contours by eye. The current
pass starts from same-source VTracer candidates and limits shape changes to
source-evidenced circles and visibly straight segments. Its larger square
canvas also preserves the source composition and whitespace instead of
silently zooming the subject.

## Quick start

Create a schema-valid reconstruction-map draft with the confirmed target,
viewport, normalization decision, components, constraints, and confirmation
records. Freeze it, run the required same-source VTracer sweep described in
`references/vtracer-workflow.md`, select by metric plus overlay/diff, and apply
only the allowed primitive corrections. Then run the immutable evaluation
stages:

```bash
.venv/bin/python scripts/prepare_reference.py \
  --source source.png --draft draft.json --output work --freeze

.venv/bin/python scripts/evaluate_icon.py \
  --map work/reconstruction-map-r01.json \
  --candidate candidate.svg --iteration 0 --run-dir work/run-i00

.venv/bin/python scripts/finalize_review.py \
  --evaluation work/run-i00/evaluation-i00.json \
  --semantic-review semantic-review.json \
  --output deliverables/acceptance-report.json
```

See `references/reconstruction-workflow.md` for the complete workflow,
`references/vtracer-workflow.md` for tracing, selection, and postprocessing,
`references/acceptance-model.md` for formulas and gates, and
`references/security-and-rendering.md` for the input and renderer boundary.

## Outputs

- a frozen `reconstruction-map-rNN.json` and reference masks;
- immutable `evaluation-iNN.json` reports and run diagnostics;
- an editable candidate SVG;
- a schema-valid `acceptance-report.json` after semantic review;
- an honest status: `accepted`, `not_accepted`, `incomplete`,
  `non_canonical`, `invalid_input`, or `runtime_error` as applicable.

The final wording is a score such as “98.12/100 under acceptance model 1.0.2
at target 98/100.” A high score never overrides a failed hard gate.

## Release archive

Build the deterministic ZIP and adjacent checksum outside the source tree:

```bash
python scripts/build_release.py \
  --source . --output /path/to/release-output --version 0.1.0
```

The builder reads only the exact paths in the committed
`release-manifest.txt`, fixes entry order, timestamps, and permissions,
rejects unsafe files and path leaks, publishes without overwriting existing
outputs, safely re-extracts the ZIP, and reruns the repository-local skill and
schema validators. The development repository retains behavioral
RED/GREEN evidence. The ZIP omits `tests/behavioral/evidence/` because that
directory combines summaries with verbatim agent-run records and local
harness provenance; reusable scenario prompts remain in the archive.

## Current limitations

- Reconstruction is monochrome-only in `0.1.0`.
- Candidate generation requires VTracer; no manual or alternate-tracer fallback
  is part of this workflow.
- Semantic gates require human review; `not_evaluated` cannot become
  `accepted`.
- Baseline plus at most eight refinements is the default; stalled or exhausted
  runs stop without lowering the target or tolerances.
- Canonical acceptance requires the locked renderer and supported isolation.
- Node `22.14.0` does not provide a network permission scope. The canonical
  runner therefore imports no network module, initializes the pinned loader
  from already-read WASM bytes, and rejects external SVG resources, but the
  contract does not claim OS-level network denial.
- Publication readiness is **BLOCKED** until the mandatory live Ubuntu x64
  canonical CI job completes with zero skips; see
  `docs/releases/v0.1.0.md` for the explicit verification boundary.
- No GitHub remote, tag, release, or package publication is created by these
  files.

The repository's original code is Apache-2.0 licensed. Third-party components
retain their own licenses; see `THIRD_PARTY_NOTICES.md` and
`docs/provenance.md`.
