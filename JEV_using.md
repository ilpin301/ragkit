# Jev (TypeSafe AI) — fit for our RAG stack

Assessed 2026-09-23. Source: https://typesafe.ai/ (Jev released ~2026-09-19). Vendor claims unverified.

## What it is
- "System One" model: no text generation. Input = state + question → typed decision (bool / enum / number) + calibrated probability.
- Separate *decisions* endpoint, not chat completions.
- Claims: ~190x faster, $0.042 / 1M input tokens, output free, "zero hallucinations" (because no free text).
- Training: RLCD (Reinforcement Learning for Calibrated Decisions).

## Verdict
Not useful for the heavy parts of our LightRAG stack. Only niche wins. Not worth adopting now; revisit if we add cross-base routing or a rerank step.

| Pipeline stage | Needs | Jev fit |
|---|---|---|
| Entity/relation extraction at ingest (GLM) | generated text | No — main cost, Jev can't do it |
| Embeddings | vectors | No |
| Answer synthesis | generated text | No |
| Rerank / filter retrieved chunks | yes/no + prob | Yes |
| Query routing (mode or base) | enum | Yes |
| Ingest triage (junk / off-topic / dup) | classification | Yes |
| Answer grounding check | bool + confidence | Plausible |

## Where it would help — concrete situations

### Relevance filter / reranker
Ask per retrieved chunk: "does this chunk help answer Q?" → keep if prob > threshold.
1. **Noisy hybrid retrieval** — 20 chunks back, 6 off-topic (generic mentions from unrelated papers) → drop them; smaller, cleaner GLM prompt, less cross-paper number mixing.
2. **Large top_k for recall** — retrieve 60, keep those > 0.7 → recall without huge prompt.
3. **VLM caption noise** — "Fig. 3: SEM image" chunks match keywords but carry nothing → filtered.
4. **"Not in base" detection** — all chunks < 0.3 → answer "no source covers this" instead of GLM improvising. Relies on calibrated probabilities. *Highest-value use.*

### Base routing (PCM 9622 / MECH 9623 / CHEM 9621)
1. **Single entry point** — one query skill; Jev returns enum `PCM | MECH | CHEM | multiple` → query right base(s). *Second highest-value use.*
2. **Cross-domain questions** — e.g. PCM thermal expansion vs housing mechanical load → `multiple` → query PCM + MECH, merge.
3. **Query mode pick** — enum `local | global | hybrid | naive`: specific fact → local, corpus-wide overview → global. Cheaper than always hybrid.
4. **Ingest routing** — PDF in shared IN/ → which base, or junk; low confidence → ask user.

## When NOT worth it
- One base per session, already known → routing adds nothing.
- top_k already small (5–10) and answers fine → filter just adds latency + one more API dependency.
- One cheap GLM call returning JSON can do the same classification; Jev wins only if its speed / calibration matters at our volume.

## Links
- https://typesafe.ai/blog/introducing-system-one-models-and-jev
- https://www.marktechpost.com/2026/09/19/typesafe-ai-releases-jev/
- https://apimaster.ai/blog/jev-api
- https://www.datacamp.com/blog/system-one-models-jev
