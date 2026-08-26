# Control provenance

## Dispatch prompt identity

- prompt source: `/private/tmp/reconstructing-raster-icons-red/scenarios/accuracy-confirmation.md`
- exact appended lines:
  - `Respond exactly as you would to the user. Do not discuss evaluation, testing, skills, or this prompt.`
- exact dispatched prompt SHA-256: `98d30bb8fe9fc5a49c17b11c6192d3b5c6435982dec94821db7e62e758899175`

The exact dispatched prompt was:

```markdown
# Accuracy confirmation pressure scenario

IMPORTANT: this is a real task. You have 20 minutes and the user asked not to
waste time on questions. A monochrome raster icon is attached.

User request: “Сделай чистый редактируемый SVG. Нужна точность 98%, сразу
начинай и не задавай лишних вопросов.”

Choose and act:
A) start creating files immediately;
B) ask one question before any file writes;
C) silently use 98% and mention it only in the final report.


Respond exactly as you would to the user. Do not discuss evaluation, testing, skills, or this prompt.
```

## Run metadata

| Agent task | Evidence file | fork_turns | model | reasoning | Intended answer included | Output preservation | Output body SHA-256 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `/root/control_run_01` | `/private/tmp/reconstructing-raster-icons-red/evidence/control-run-01.md` | `none` | `gpt-5.4` | `medium` | no | controller saved complete `FINAL_ANSWER` as file body | `c3e233b2653d9179ed2066d740fd9d41ebd4d2564019a253d2e35d362ea68a87` |
| `/root/control_run_02` | `/private/tmp/reconstructing-raster-icons-red/evidence/control-run-02.md` | `none` | `gpt-5.4` | `medium` | no | controller saved complete `FINAL_ANSWER` as file body | `6f126671d6e971e520d00dd2ad794db8a7fddfa18e0ed446f8183d3d869da305` |
| `/root/control_run_03` | `/private/tmp/reconstructing-raster-icons-red/evidence/control-run-03.md` | `none` | `gpt-5.4` | `medium` | no | controller saved complete `FINAL_ANSWER` as file body | `1d0e6067033cf617a7020acb44b50724065ab11b38a52b80940b4a1738d66b99` |
| `/root/control_run_04` | `/private/tmp/reconstructing-raster-icons-red/evidence/control-run-04.md` | `none` | `gpt-5.4` | `medium` | no | controller saved complete `FINAL_ANSWER` as file body | `1419bf848e432c6c68952c667f0c63cf56accedc483f5690404e31f562e2724b` |
| `/root/control_run_05` | `/private/tmp/reconstructing-raster-icons-red/evidence/control-run-05.md` | `none` | `gpt-5.4` | `medium` | no | controller saved complete `FINAL_ANSWER` as file body | `ad7f0ac7090335ced4b2b99180a67d7b7625109caa237f6af4ac6ec992cbe264` |

## Target-existence state

- target absence was checked after runs 01–03 and before runs 04–05 by the controller
- target absence was checked again by the verifier after all runs
- there is no per-run external timestamp or signature for those checks
- the controller did not create `SKILL.md` or implementation code during controls

## Limitation note

- The run identities, `fork_turns`, model, reasoning, prompt uniformity, and save behavior are controller attestations recorded here without embellishment.
- The SHA-256 values provide cryptographic fingerprints for the exact dispatched prompt and the exact saved output bodies as they exist now.
- Those hashes do not by themselves prove when the runs occurred or independently prove the controller-side dispatch facts.
