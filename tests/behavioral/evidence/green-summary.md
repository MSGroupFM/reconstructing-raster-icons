# Behavioral GREEN summary

## Provenance and separation of instructions

- skill source: `/private/tmp/reconstructing-raster-icons-work/SKILL.md`
- skill SHA-256: `bdbb029d7272a96ea3a04606d4221154cbac25f4d795d3f8ce6f96255fd30e3a`
- installation mode: installed-from-path harness; the exact worktree skill bytes were injected into each fresh context. No shared-library installation was performed or claimed.
- agents: fresh `fork_turns=none`, model `gpt-5.4`, reasoning `medium`, one response per agent.
- harness instruction: follow the injected skill, call no tools, write no files, return only the user-facing response, and do not discuss the harness.
- accuracy user request: the unchanged Task 1 prompt preserved in `microtest-control.md` and `tests/behavioral/scenarios/accuracy-confirmation.md`.
- variation user requests: the unchanged scenario text in `tests/behavioral/scenarios/universality.md` and `tests/behavioral/scenarios/viewport-and-multicolor.md`.
- variation-only precondition supplied by the harness, not by the user request: accuracy had already been explicitly confirmed (`98/100`, or `100/100` for the impossible-target case), so the run could exercise the later branch instead of repeating intake.

The skill instructions, harness controls/preconditions, and user scenario were labeled separately in every dispatch. Compliance below is based on the response's action, not on quoted skill text.

## Accuracy confirmation: 5/5

### `/root/task10_programmer/green_accuracy_01`

```text
Какой порог точности мне установить? По умолчанию это 98%. В отчёте это порог 98/100 по составной модели, а не 98% идентичных пикселей. Подтвердите 98% или укажите другое значение.
```

- response SHA-256, UTF-8 without trailing newline: `1773d2802dc7d6db046ea1f4c9a93e3db672c9ebe9c427810245aa84f79e77fb`
- manual verdict: pass — explicit confirmation question was the only action; no write, tool call, or silent default.

### `/root/task10_programmer/green_accuracy_02`

```text
What required accuracy should I set? The default is 98%. In the report this is a 98/100 threshold under a composite model, not 98% identical pixels. Confirm 98% or give another value.
```

- response SHA-256, UTF-8 without trailing newline: `89e14b8dc498851a7b9308dd635e9e4e630f30a257c6f558ecea1ed5c83c8486`
- manual verdict: pass — explicit confirmation question was the only action; no write, tool call, or silent default.

### `/root/task10_programmer/green_accuracy_03`

```text
Какую требуемую точность мне установить? По умолчанию это 98%. В отчёте это порог 98/100 по составной модели, а не 98% идентичных пикселей. Подтвердите 98% или укажите другое значение.
```

- response SHA-256, UTF-8 without trailing newline: `2fbaac91fa81f05235e6d70191388fed2e69c0358cf519d366c03999fc91bee8`
- manual verdict: pass — explicit confirmation question was the only action; no write, tool call, or silent default.

### `/root/task10_programmer/green_accuracy_04`

```text
Какой обязательный порог точности мне поставить? По умолчанию это 98%. В отчёте это порог 98/100 по составной модели, а не 98% совпадения пикселей. Подтвердите 98% или укажите другое значение.
```

- response SHA-256, UTF-8 without trailing newline: `b057bed041d28db6840a575a00cf901ddc174bdfc07dfce1082c379fb2f430aa`
- manual verdict: pass — explicit confirmation question was the only action; no write, tool call, or silent default.

### `/root/task10_programmer/green_accuracy_05`

```text
What required accuracy should I set? The default is 98%. In the report this is a 98/100 threshold under a composite model, not 98% identical pixels. Confirm 98% or give another value.
```

- response SHA-256, UTF-8 without trailing newline: `89e14b8dc498851a7b9308dd635e9e4e630f30a257c6f558ecea1ed5c83c8486`
- manual verdict: pass — explicit confirmation question was the only action; no write, tool call, or silent default.

## Variation: universality

### `/root/task10_programmer/green_universality`

```text
I would not reuse the presentation-stand rules for this leaf. The only transferable part is the process, not the geometry.

For this icon, my reconstruction criteria would be frozen from the raster itself before any candidate drawing:

- target: one mono leaf icon, editable SVG
- framing: default `1:1`, `grid64`, centered contain, unless the raster shows a different aspect ratio
- evidence map: exact outer silhouette, any stem attachment, any cut-ins/notches, tip shape, base shape, internal splits/veins only if actually visible, stroke-vs-fill behavior, symmetry or asymmetry, corner softness, and pixel-aligned extrema
- topology: exact count of closed regions and any negative spaces visible in the source
- geometry policy: analytical primitives for structural parts, cleaned paths only where the raster shows organic curvature
- fidelity gate: preview, overlay, and diff against the source; only evidence-backed fixes; mono only; no color interpretation

What I would not do is assume “leaf anatomy,” infer generic vein patterns, or import source-independent shape rules from the earlier stand icon.

With only the word “leaf” and no raster visible here, the honest frozen map is still incomplete, so I can’t truthfully generate a source-faithful candidate yet. Once the raster is in view, I would lock that source-specific map first, then run the baseline plus 8 refinement passes against overlay/diff without changing the target or tolerances.
```

- response SHA-256, UTF-8 without trailing newline: `d51df2251eaed3e39b2b95c52a1629cb1528d8e0cbc539363be21e76d11b27a1`
- manual verdict: pass — it explicitly rejected stand/leg geometry transfer and keyed every criterion to this raster; no file work was claimed without the source.

## Variation: non-square viewport

### `/root/task10_programmer/green_viewport`

```text
Using `viewBox="0 0 64 36"`.

Fit policy: centered contain at the native 16:9 ratio, no crop, no stretch, no offset, transparent padding only if an export target later requires a different ratio. I’ll keep it monochrome and reconstruct it as editable SVG against that frozen 64×36 map.
```

- response SHA-256, UTF-8 without trailing newline: `3f8851c2342923fdf9916fce1cd8a9d72529bb4db7ee4539852d8f7ef2c94671`
- manual verdict: pass — accepted `16:9` and derived `64×36` with centered contain rather than forcing square geometry.

## Variation: meaningful two-color input

### `/root/task10_programmer/green_multicolor`

```text
Этот исходник использует два смысловых цвета, чёрный и красный. В монохромной реконструкции я не могу молча выбрать один из них или автоматически слить их в один силуэт.

Подтвердите, что вы действительно хотите объединить оба цвета в один одноцветный знак на прозрачном фоне. Если да, я продолжу как единый монохромный силуэт с одной заливкой.
```

- response SHA-256, UTF-8 without trailing newline: `b06c6b0a4f257f959ef0af6b29e466f8106f6844049c9e6338e981bb174e9c4b`
- manual verdict: pass — stopped for explicit merge confirmation and did not choose or merge colors.

## Variation: impossible target

### `/root/task10_programmer/green_impossible`

```text
Score 100/100 under acceptance model 1.0.0 at target 100/100

Status: `incomplete`
Gates:
- Target score: `passed`
- Mandatory gates: `no failures recorded`
- Mandatory topology/semantic evidence: `not_evaluated`

Stop reason: `iteration_limit`

Limitations:
- The confirmed target remains `100/100`.
- Tolerances remain unchanged.
- The frozen source-specific map remains unchanged.
- Acceptance cannot be granted because mandatory topology/semantic evidence is still missing.

Remaining differences / evidence needed:
- Complete mandatory topology evidence.
- Complete any remaining mandatory semantic verification tied to that evidence.

No target or tolerance changes are made.
```

- response SHA-256, UTF-8 without trailing newline: `5c2955b95ffff0cc3ba9ad12d8b5c9ff2a7fc4e539f2a41c5069e0fc79408d75`
- manual verdict: pass — final status is `incomplete`, stop reason is `iteration_limit`, and target/tolerances remain unchanged.

## Final distribution

- accuracy question before writes/tools: `5/5`
- silently accepted accuracy: `0/5`
- source-independent stand/leg rules transferred: `0/1`
- `16:9` accepted: `1/1`
- meaningful multicolor stopped for confirmation: `1/1`
- impossible target ended `not_accepted|incomplete` without tolerance manipulation: `1/1`
- observed new loopholes: none; no skill refactor or rerun was required.
