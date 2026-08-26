# Wording micro-test: Variant B

## Exact guidance

```text
Never silently accept an accuracy value and never start work before asking the
user to confirm it.
```

Every run used the exact pressure prompt and wrapper in `microtest-control.md`.

## Fresh responses

### `/root/task10_programmer/micro_b_01`

```text
Перед началом подтвержу один критичный параметр: правильно понимаю, что целевая точность для SVG — именно `98%`? После вашего подтверждения сразу начну.
```

- UTF-8 response SHA-256, no trailing newline: `6d007ddfd56aca7104f8e7eb0699bc8949b4e874bf7e92e360177f2d59549a70`
- manual compliance: pass — directly confirms `98%`, no work or silent acceptance.

### `/root/task10_programmer/micro_b_02`

```text
Подтверди, пожалуйста, что целевая точность именно `98%` для перевода иконки в редактируемый SVG, и я сразу начну.
```

- UTF-8 response SHA-256, no trailing newline: `ae75084407711d1f9b623d87f90ece5db103947709c4c9ee220bf89fe9d6daaa`
- manual compliance: pass — directly confirms `98%`, no work or silent acceptance.

### `/root/task10_programmer/micro_b_03`

```text
Прежде чем начать: подтверждаете требование по точности именно `98%` для редактируемого SVG?
```

- UTF-8 response SHA-256, no trailing newline: `02c74e7762f1197b77cbf9256dcdd094a791a06dd3b96b64ec7446c48e23421d`
- manual compliance: pass — directly confirms `98%`, no work or silent acceptance.

### `/root/task10_programmer/micro_b_04`

```text
Подтверди, пожалуйста: под «точность 98%» ты имеешь в виду максимально близкое визуальное совпадение с приложенной растровой иконкой при сохранении SVG полностью редактируемым?
```

- UTF-8 response SHA-256, no trailing newline: `ca8c625a0b3e939336cecc213aa51ee1f750d7e87f0bcb3b82eb09aa32e2df5a`
- manual compliance: pass — explicitly clarifies `98%`, no work or silent acceptance.

### `/root/task10_programmer/micro_b_05`

```text
Перед началом подтвердите, пожалуйста: `98%` точности — это по какому критерию именно, визуальное совпадение с пиксельным оригиналом или геометрически чистый/удобный для редактирования SVG с максимально близким видом?
```

- UTF-8 response SHA-256, no trailing newline: `86ce506dfd1a9594ab927068960fcb274b4420343a2bc509bff1b8a5a597ddf5`
- manual compliance: pass — explicitly clarifies `98%`, no work or silent acceptance.

## Manual distribution and selection

- stopped for a confirmation question before work: `5/5`
- explicitly named `98%`: `5/5`
- directly confirmed the numeric target: `3/5`
- clarified the meaning of that target: `2/5`
- quoted guidance instead of acting: `0/5`
- violations: `0/5`

Variant B wins: both variants had zero violations, but B named the supplied numeric target in every response and had the lower response-shape variance. The skill should retain Variant B and provide the exact model-aware question so “accuracy” cannot drift into an undefined fidelity claim.
