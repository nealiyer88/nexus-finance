# Skeptic — Argument Against Feature 8a as Specified

## 1. The scope bundles two unrelated retrofits

fastText (Stages 2c + Signal Set C) and Signal Set B reconciliation are **independent**. B-signals fire in the 0.70–0.90 ambiguous band and read from `entity_edges`, `entity_aliases`, `external_fields` JSON — none of that touches embeddings. Yet 8a bundles them, modifying `blocking.py`, `indices.py`, `scoring.py`, `weights.py`, and adding `embeddings.py` + a fetch script + three test surfaces in one L feature. This is precisely the "while I'm here" scope creep called out in the known failure modes: a 1-file abbreviation fix turns into a 5-file diff nobody reviews cleanly.

**Alternative:** ship 8a as fastText-only (Stage 2c + Signal Set C). Fold Signal Set B reconciliation into 8b, alongside B3 + the transactions table. B6 already ships; the +0.20 cap can be applied there as a 3-line fix without touching B1/B2/B4/B5.

## 2. The abbreviation-lift test is self-fulfilling

The success criterion `"pacrim tech" ↔ "pacific rim technologies international"` scores above 0.70 with Signal Set C enabled and below 0.70 without. But Implementation Note 4 says **tests monkeypatch `embed` or load a tiny vendored stub vector table**. So the assertion becomes: "the test-author's hand-crafted stub vectors produce the expected cosine on the fixture pair." That proves nothing about actual `compress-fasttext` behavior on real Common Crawl vectors. This is the "false PASS from reading code without executing" pattern — the test passes because someone wrote the stub to make it pass. A single optional integration test "skipped if model absent" does not close this gap.

**Concrete risk:** CI never exercises the real model, dev machines rarely rerun it, and the first time reality hits the fixture is post-merge.

## 3. B2 rides on utilities the brief hand-waves

B2 reads `system_references.external_fields` JSON, extracts project-code segments "via the existing project-code shape utility," and matches "via `_check_psa_abbreviation`'s shortcode logic." That's three named-but-unread utilities glued together in the 0.70–0.90 gate. If any of them has a slightly different contract than the brief assumes (case sensitivity, segment delimiters, PSA shortcode length), B2 fires wrong-direction boosts silently — and the +0.20 cap masks it. The success criterion "≥4 B-signals trip and cap holds at exactly +0.20" does not distinguish "B2 correctly fired" from "B2 incorrectly fired but got capped."

## 4. The 95% gate is unmeasurable until 8b + feature 12 land

The brief admits the 95% figure is measured by the orchestrator (feature 12) over real cycles, and 8b is the prerequisite. So 8a ships an abbreviation-lift capability whose *actual success* — hitting the auto-match gate — cannot be measured until two more features land. The two fixture pairs in `test_scoring.py` are not a gate; they're a smoke test that can be trivially satisfied by tuning the Signal Set C weight in `WeightConfig` until those exact strings cross 0.70. Overfit risk is real.

## 5. The B3 defensive-guard block is a symptom, not a fix

Requiring reviewers to grep for `amount_cooccurrence` / `transaction` encodes tribal knowledge into a review checklist. The prior attempt shipped no-op B3 scaffolds *despite* B3 being out of scope. Adding a grep-rule to the brief does not prevent recurrence — the next retrofit author reads a different version of the brief. The right fix is a `_compute_b_boosts` signature that structurally cannot accept transaction data (e.g., typed inputs enumerating {B1, B2, B4, B5, B6} only). Without that, this feature will ship the same no-op scaffolds again.

**Recommendation:** split into 8a (fastText only) + 8b (transactions table + B3 + full Signal Set B reconciliation). Two smaller diffs, each independently reviewable, and the B-signal work lands with the real transactions data that lets it be tested against non-synthetic pairs.