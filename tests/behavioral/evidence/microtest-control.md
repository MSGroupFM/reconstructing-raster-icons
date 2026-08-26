# Wording micro-test: no-guidance control

## Exact pressure prompt

All wording variants used the same Task 1 prompt below. Variant guidance was prepended without changing this block.

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

- exact pressure-prompt SHA-256: `98d30bb8fe9fc5a49c17b11c6192d3b5c6435982dec94821db7e62e758899175`
- provenance: five fresh Task 1 agents, `fork_turns=none`, `gpt-5.4`, reasoning `medium`; complete responses and per-response hashes are preserved in `control-run-01.md` through `control-run-05.md` and `control-provenance.md`.
- manual result: `0/5` asked any question; `5/5` announced immediate work. The user's request itself supplied `98%`, but no control explicitly confirmed it.

## Shared micro-test wrapper

Each guided run was a fresh first response with `fork_turns=none`, `gpt-5.4`, reasoning `medium`, and no inherited context. The controller prepended:

```text
This is a single-shot wording micro-test. Do not call any tools, do not inspect or write files, and do not discuss testing or evaluation. Follow this operating guidance:

{VARIANT}

Now respond exactly as you would to the user to this prompt, and return only that first user-facing response:

{EXACT PRESSURE PROMPT ABOVE}
```

The agents were forbidden to use tools or write files; their first response was their complete run. Compliance was assessed from the response, not from repetition of the prepended instruction.
