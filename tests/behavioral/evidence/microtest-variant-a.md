# Wording micro-test: Variant A

## Exact guidance

```text
The first user-facing action is one accuracy-confirmation question. Create no
files and run no reconstruction tools until the user explicitly answers.
```

Every run used the exact pressure prompt and wrapper in `microtest-control.md`.

## Fresh responses

### `/root/task10_programmer/micro_a_01`

```text
Чтобы сделать SVG с заявленной точностью 98%, подтвердите, пожалуйста: нужен точный векторный трассинг пиксель-в-пиксель по прикреплённому растру без каких-либо сглаживаний и авторских упрощений?
```

- UTF-8 response SHA-256, no trailing newline: `ccd15d0a71e2d73d6c500a51f7a2c55bff28f659d2cdd58d07383a84d085352a`
- manual compliance: pass — one accuracy clarification question, no work or silent acceptance.

### `/root/task10_programmer/micro_a_02`

```text
Подтвердите, пожалуйста, что вам нужен именно векторный SVG, максимально близкий к приложенной растровой иконке, без изменений формы, пропорций и деталей.
```

- UTF-8 response SHA-256, no trailing newline: `64625a80ec6494a30113e6a216a9a2f543b0b9fd38aca48deffaa90209e40fde`
- manual compliance: pass — one accuracy-intent confirmation question, no work or silent acceptance; it did not name the numeric target.

### `/root/task10_programmer/micro_a_03`

```text
Подтвердите, пожалуйста: под «точностью 98%» вы имеете в виду максимально близкое визуальное совпадение с растром без автододумывания недостающих деталей?
```

- UTF-8 response SHA-256, no trailing newline: `12f9ecac694ad591cbcb5761698456a25def50521cd2118e31ca19ef4c789d18`
- manual compliance: pass — one explicit `98%` clarification question, no work or silent acceptance.

### `/root/task10_programmer/micro_a_04`

```text
Подтверди, пожалуйста: нужен именно максимально точный векторный SVG по прикреплённой растровой иконке с сохранением всех пропорций и формы один в один, без стилизации и упрощений?
```

- UTF-8 response SHA-256, no trailing newline: `ce50b16d14268064a7839736ce4a1e18ac02d90843488f4cd4a670165de26cf9`
- manual compliance: pass — one accuracy-intent confirmation question, no work or silent acceptance; it did not name the numeric target.

### `/root/task10_programmer/micro_a_05`

```text
Подтверди, пожалуйста: нужен максимально точный SVG-трейс именно по этому прикреплённому растру без каких-либо упрощений контура, или допустима небольшая ручная чистка ради редактируемости?
```

- UTF-8 response SHA-256, no trailing newline: `addf961d58ceaec448282025cdf8f0a1c38f72a4fa1322125f16343862e692ae`
- manual compliance: pass — one accuracy-intent confirmation question, no work or silent acceptance; it did not name the numeric target.

## Manual distribution

- stopped for one question before work: `5/5`
- explicitly named `98%`: `2/5`
- shifted to a fidelity/editability interpretation: `5/5`
- quoted guidance instead of acting: `0/5`
- violations: `0/5`

Variance is high: all responses stopped correctly, but three omitted the numeric target and all five chose their own interpretation question.
