# Reconstructing Raster Icons

`reconstructing-raster-icons` is a Codex skill and deterministic validation
toolkit for rebuilding a raster icon as editable SVG. It records the source
structure before drawing, renders the candidate with a hash-pinned WASM
renderer, and reports a composite fidelity score plus blocking automatic and
semantic gates.

Version `0.1.0` is prepared for monochrome icons only: one opaque foreground
color on a transparent background. Meaningful multicolor, gradients,
intentional translucency, photographs, and painterly images stop for an
explicit scope decision instead of being silently flattened.

## Requirements

- Python `3.11`, `3.12`, or `3.13`;
- Node.js `22.14.0` for dependency installation;
- the exact npm graph in `package-lock.json`, including
  `@resvg/resvg-wasm@2.6.2` and the platform-pinned Node binary;
- Python packages from `requirements-lock.txt`.

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
a threshold on acceptance model `1.0.0`, not a percentage of identical pixels.

## Quick start

Create a schema-valid reconstruction-map draft with the confirmed target,
viewport, normalization decision, components, constraints, and confirmation
records. Then run the immutable stages:

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
`references/acceptance-model.md` for formulas and gates, and
`references/security-and-rendering.md` for the input and renderer boundary.

## Outputs

- a frozen `reconstruction-map-rNN.json` and reference masks;
- immutable `evaluation-iNN.json` reports and run diagnostics;
- an editable candidate SVG;
- a schema-valid `acceptance-report.json` after semantic review;
- an honest status: `accepted`, `not_accepted`, `incomplete`,
  `non_canonical`, `invalid_input`, or `runtime_error` as applicable.

The final wording is a score such as “98.12/100 under acceptance model 1.0.0
at target 98/100.” A high score never overrides a failed hard gate.

## Release archive

Build the deterministic ZIP and adjacent checksum outside the source tree:

```bash
python scripts/build_release.py \
  --source . --output /path/to/release-output --version 0.1.0
```

The builder fixes entry order, timestamps, and permissions; rejects symlinks
and path leaks; safely re-extracts the ZIP; and reruns the repository-local
skill and schema validators. The development repository retains behavioral
RED/GREEN evidence. The ZIP omits `tests/behavioral/evidence/` because that
directory combines summaries with verbatim agent-run records and local
harness provenance; reusable scenario prompts remain in the archive.

## Current limitations

- Reconstruction is monochrome-only in `0.1.0`.
- Semantic gates require human review; `not_evaluated` cannot become
  `accepted`.
- Baseline plus at most eight refinements is the default; stalled or exhausted
  runs stop without lowering the target or tolerances.
- Canonical acceptance requires the locked renderer and supported isolation.
- Live Linux-x64 canonical CI execution is not claimed by the prepared source
  tree; see `docs/releases/v0.1.0.md` for the explicit verification boundary.
- No GitHub remote, tag, release, or package publication is created by these
  files.

The repository's original code is Apache-2.0 licensed. Third-party components
retain their own licenses; see `THIRD_PARTY_NOTICES.md` and
`docs/provenance.md`.
