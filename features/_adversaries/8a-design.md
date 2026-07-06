# Design Advocate: fastText + Signal Set B Retrofit (Feature 8a)

## 1. Why this design is correct

The brief solves a **specific, measured gap** with a **specific, measured signal**. Product spec v4 raised the Phase 1 gate to 95%, and the fixture proves the gap is real: `pacrim tech` ↔ `pacific rim technologies international` scores near-zero on `token_set_ratio` and trigram Jaccard because those metrics operate on character-surface overlap, not subword semantics. fastText's subword vectors are *literally the algorithm designed for this class*: OOV abbreviations decompose into n-gram vectors that share basis with their expansions. No other V1-legal signal has this property. RapidFuzz can't invent characters that aren't there.

The **flat-cosine choice** is also correct. At <500 canonicals a numpy dot-product scan is sub-millisecond. Adding faiss or hnswlib introduces a C++ build surface on Apple Silicon CI for zero measurable latency win. `.claude/rules/01-nexus-finance-v1.md §11` already flags "re-evaluate at >50K entities" — flat scan honors that boundary exactly.

The **compress-fasttext + quantized cc.en.300** choice avoids the two failure modes that would sink this: a 7GB model in CI, and a compiled C++ pip whose wheels break on M-series Macs. Pure-Python loader + <100MB artifact + gitignored `models/` + idempotent fetch script is the minimum viable footprint.

## 2. Why the scope is right

The Signal Set B **partitioning at B3** is the single sharpest scoping decision in the brief. B3 (amount co-occurrence) requires a `transactions` table that does not exist in `db/schema.sql` or `db/schema_sqlite.sql`. Bundling B3 into 8a would force a schema change into an embedding-integration feature — two unrelated risks compounded. Deferring B3 to 8b keeps 8a's blast radius contained to `embeddings.py`, `indices.py`, `blocking.py`, `scoring.py`, `weights.py`. That's five files, all matcher-owned per rules §12.

The **defensive grep guard** ("reviewers MUST grep for `amount_cooccurrence`, `transaction`") is unusual but earned — CC-LEARNINGS 2026-06-21 records a prior attempt that shipped no-op B3 scaffolds. The guard prevents a repeat at zero implementation cost.

Signal Set C **as a weighted ensemble entry** (not a replacement for RapidFuzz) is also right. fastText cosine on `Cenlar, LLC` vs `Cenlar FSB` would give a strong but non-decisive signal; blending it with `token_set_ratio` via `WeightConfig` per category-pair preserves interpretability — exactly what rules §11 promised when it rejected XGBoost.

## 3. Why now

Feature 12 (matcher-orchestrator) measures the 95% gate over real cycles. The orchestrator cannot demonstrate the gate without both the bridge signal (fastText) *and* the corroboration signals (B1/B2/B4/B5/B6 auditable). Shipping 8a before 12 is the only ordering where 12 has a measurable target. 8b (B3) follows 8a but does not gate it, because B3 lifts marginal recall — it doesn't unblock the 80→95 bridge that 8a does.

## 4. Risks of NOT building this

- **95% gate is unprovable.** Feature 12 ships with a suite that cannot pass its own success criterion. Every downstream feature that assumes the gate holds inherits an unfounded assumption.
- **Abbreviation classes stay in the surface-to-no-match band (0.50–0.70).** Every `pacrim`-shaped pair either escalates to Tier 3 LLM (cost blowout past the <15% ceiling in rules §1) or gets missed.
- **Signal Set B remains un-auditable.** v4 §9 mandates per-boost logging; without the reconciliation, auto-approves in the 0.70–0.90 band are opaque to review — a compliance surface, not just an engineering one.

Build it as specified.