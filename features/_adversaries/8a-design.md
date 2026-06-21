# Design Advocate: Argument FOR the Brief

## 1. Why this design is correct

The brief identifies the **one** signal that bridges the v4 80→95 accuracy gap and refuses to bolt on anything else. `pacrim tech` ↔ `pacific rim technologies international` and `meridian cap` ↔ `meridian capital group` are not edge cases — they are the canonical PSA-shortcode ↔ accounting-full-name pattern that defines QB↔RUDDR resolution. RapidFuzz `token_set_ratio` scores these near zero; trigram Jaccard does no better because the shared subword surface is tiny relative to the full string. Subword embedding cosine is mathematically the right tool, and pre-trained `cc.en.300` already encodes "tech" ≈ "technologies" and "cap" ≈ "capital" without a corpus. The brief picks the only signal class that solves the actual failure mode.

It also respects every binding constraint in `.claude/rules/01-nexus-finance-v1.md`: pre-trained only (no fine-tuning, §11), flat cosine at <500 canonicals (no faiss/annoy, sub-ms anyway), `compress-fasttext` quantized loader (no 7GB model, no C++ build on Apple-Silicon CI), gitignored `models/` with idempotent fetch script, monkeypatched embeddings in unit tests. Each of these is a `Why: previous incident` decision, not preference — the C++ fasttext pip wheel is a known Apple-Silicon CI breaker, and a 7GB model would silently destroy contributor onboarding.

## 2. Why the scope is right

The shape of the pipeline does not change. Same six stages, same `ConnectorInterface`, same `0.90 / 0.70 / 0.50` cutoffs, same `WeightConfig` dispatch pattern. The brief extends `indices.py`, `blocking.py`, `scoring.py`, `weights.py` — files already owned by this subsystem per §12 — and adds exactly one new module (`embeddings.py`) plus one fetch script. Signal Set B reconciliation is in-scope because it ships in the same `scoring.py` edit and v4 §9 demands the +0.20 cap + per-signal logging that V3 shipped without; doing it later means re-opening `score_pair` twice. Cutting B reconciliation out would be smaller but worse — it would leave `score_pair` half-conformant to v4 and force feature 12 to defend against unaudited boosts.

What is correctly excluded matters as much: no ANN library, no fine-tuning, no Layer-3 contextual embeddings, no disposition-threshold changes (the 95% is an orchestrator gate, not a constant — the brief calls this out explicitly so the next contributor does not "fix" the 0.90 constant).

## 3. Why now

This is a retrofit blocker. Features 7 + 8 shipped under V3 rules; feature 12 cannot land on a non-conformant matcher. Doing fastText after 12 means re-touching every Stage 3 caller twice. The dependency chain is linear and short: `compress-fasttext` library pin is the one open decision, already routed through Phase 1 engineer adversary debate (Implementation Notes §1). Every other input — fixture pairs, weights config, tenant-scoping pattern — already exists in the shipped code.

## 4. Risks of NOT building

- **Phase 1 success gate unreachable.** v4 §7 sets 95%; without Signal Set C the ceiling is ~80% on real fixtures. The product spec promotion of fastText to V1-mandatory was driven by exactly this measurement.
- **Defensibility collapses (§5).** "Cold-start works on day one" is the V1 thesis. Pre-trained fastText is the zero-corpus signal that makes day-one accuracy real; without it, the first customer's first day is the 80% experience.
- **Audit debt compounds.** Every day Signal Set B ships without the +0.20 cap and per-boost logging is a day of `signal_breakdown` rows that cannot be re-played for v4 compliance review.
- **Feature 12 gets blocked or built on sand.** Either way, schedule slip.

Build it now, build it exactly as scoped.