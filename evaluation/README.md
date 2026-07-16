# Evidence recall evaluation

This directory contains versioned, self-contained datasets for measuring evidence
selection separately from answer generation. The baseline is deliberately offline:
it does not call a language model and it does not create or use embeddings.

Run the human-readable report:

```powershell
myoutbrain evaluate-recall evaluation/recall-baseline.json
```

Run the stable JSON report for automated comparison:

```powershell
myoutbrain evaluate-recall evaluation/recall-baseline.json --format json
```

The command exits with status `1` when any case fails. A result records correct
hits, key omissions, incorrect citations, and cases where retrieval supplied
evidence even though the system should refuse to answer.

## Dataset schema

Each dataset has `schema_version: 1`, an `evidence` array, and a `cases` array.
An evidence item has a stable `id`, a domain `kind` (`source` or
`personal-cognition`), an `active` or `superseded` state, and retrieval text. A
case has a stable `id`, one of the four baseline categories
(`answerable`, `unanswerable`, `conflicting`, or `superseded`), a question, an
`answerable` boolean, and `expected_evidence_ids`.

The current lexical retriever is the explicit no-embedding baseline. Its retrieval
decision contains both selected evidence and an explicit should-refuse signal, so
evidence presence is not treated as an answer. Future retrieval adapters implement
the shared `EvidenceRetriever` interface and reuse these datasets unchanged. There is no
corpus-wide production retriever in V1: `ask` intentionally receives one source, as
specified by the V1 scope. When such a production retriever is introduced, it uses
this shared contract rather than a second evaluation-only shape. Embeddings,
reranking, or other RAG components should be added
only after repeated, representative evaluation failures show that the lexical
baseline is inadequate.
