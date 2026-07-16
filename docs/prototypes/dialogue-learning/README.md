# Dialogue Learning Prototype

> PROTOTYPE — throwaway, in-memory only. Do not treat this as the production
> conversation ingestion or memory implementation.

## Question

Can a human-gated state model turn dialogue into compact knowledge, lesson, or
skill-like Markdown; suppress repeated extraction; and recall only accepted
experience before a future answer, without copying the full transcript into every
artifact or automatically promoting AI output?

## Run

```powershell
.\.venv\Scripts\myoutbrain-dialogue-prototype.exe
```

For a deterministic walkthrough:

```powershell
.\.venv\Scripts\myoutbrain-dialogue-prototype.exe --demo
```

If the editable install has not refreshed the script yet:

```powershell
.\.venv\Scripts\python.exe -m myoutbrain.dialogue_learning_prototype --demo
```

## What To Try

1. Capture scenario `1`, distill with `x`, and accept the lesson with `a`.
2. Capture scenario `3` and distill again. The repeated lesson should not create a
   second candidate.
3. Ask `上传 GitHub 前应该检查什么？` with `/`. Only accepted memory should appear.
4. Restart and reject the lesson with `r`; repeated distillation should preserve
   only a compact rejection fingerprint.
5. Capture small talk with `4`; it should not create a reusable artifact.

The experiment deliberately tests lifecycle and recall boundaries, not whether a
language model can write a good summary. Transcript persistence, model extraction,
privacy controls, and integration with other agents remain production design work
after the state model is validated.
