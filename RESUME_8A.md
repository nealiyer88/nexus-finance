# RESUME — 8a v2 run parked 2026-07-02 ~02:30 (delete this file in the commit that merges 8a)

## Paste into next session

```
Resuming Nexus Finance. Read RESUME_8A.md in full, then .claude/rules/01-nexus-finance-v1.md and CLAUDE.md. Make the scoring design decision it describes, finish 8a, then feature 10.
```

## State

- Branch: `rocket-run-8a-v2` (pushed to origin), parked at the commit carrying this file.
- Rocket v2 run KILLED manually at ~02:25 after the review loop overran its max-3-rounds contract (5+ review rounds, seesaw diagnosed unconvergeable). ~$18.10 spent (under $50 cap). Queue row 8a manually flipped to BLOCKED.
- HANDOFF_8a_NEXT_SESSION.md (Option D plan) still exists at repo root — Phases 0–3 executed; Phases 4–5 (PR/merge/cleanup) NOT done.
- Commit map on this branch:
  - `9eb3688` brief rewrite (Option D: 5 of 6 B-signals, B3 → 8b stub, queue rows)
  - `d6565e3` + `8740c7e` rocket.sh infra (Fable plan-tier w/ opus fallback; 5-min ROCKET_LIVE heartbeat) — reverted mid-run by fixer to clean the diff, RE-RESTORED in the park commit. Intentional and authorized.
  - `daa8518` feat Commit 1 — Stage 2c fastText blocking + Signal Set C (embeddings.py, indices.py EmbeddingIndex, blocking.py, fetch_fasttext.py, tests)
  - `cd2fbb6` feat Commit 2 — Signal Set B: B1/B2/B4/B5/B6, +0.20 cap, signal_breakdown itemization (B3 absent, structurally excluded via Literal type — the June deadlock did NOT recur)
  - `e2f1b10`, `2f28b9e` rocket fix commits (diff hygiene, cap test to 4 signals, hygiene guard restored)
- Final verdicts: QA **FAIL** / Code **PASS** (inverse of June's deadlock). Full verdicts: `features/_logs/8a-{qa,code}-verdict.md` (committed on this branch).
- 316 → 326 tests passing, 2 skipped (model-gated, by design). The suite is green; QA's FAIL is about criterion semantics, not test failures.

## THE BLOCKER — one scoring design decision, three colliding constraints

**QA-005 (BLOCKING, the real one):** brief SC-5 abbreviation lift — `"pacrim tech"` ↔ `"pacific rim technologies international"` must score >0.70 (SURFACE) with Signal C enabled. Real execution scores **0.5540**. Rounds 1–3 QA "passed" it because the test's monkeypatching was vacuous (QA-006 documents the patching defect).

Why it can't pass as built — three constraints that cannot all hold with the current scoring shape:

1. **Abbreviation lift (v4 §5 — the point of the feature):** string metrics on that pair are near-zero, so fastText must contribute ≳0.15–0.5. Its weight is 0.12 (PSA↔Accounting) — mathematically capped below what SC-5 needs.
2. **Sum-to-1.0 budget (AC-5 / round-2 CR-001, BLOCKING):** `fasttext_cosine` was stacked additively → budgets are 1.05 (default) / 1.12 (PSA). Fixing by naive proportional shrink breaks constraint 3.
3. **Threshold-pinned behavior:** `test_person_inversion_pair_scores_at_least_0_95_via_string_metrics` (rules §9: inversion pairs must clear AUTO_APPROVE=0.90) drops to 0.88 under naive renormalization, because when `embed` returns None the fastText slot contributes 0 but still consumes weight.

**Recommended resolution (decide awake, then implement):**
- **Dynamic renormalization over available signals:** when `embed` is None/model absent, exclude `fasttext_cosine` from the weighted sum AND renormalize remaining weights to 1.0. No-model path then scores exactly as pre-8a → constraint 3 holds; with-model path sums to 1.0 → constraint 2 holds.
- **Raise the fastText weight** materially for cross-category pairs (PSA↔Accounting) — it needs to be large enough that a high-cosine/low-string pair clears 0.70. Rough algebra before committing: with renormalized weights and cosine ≈0.9 on that pair, solve for the weight that puts the pair just above SURFACE without dragging exact-string pairs below AUTO_APPROVE. Sanity-check against SC-7 (brightpath/luminos must stay <0.50) and the +0.20 B-cap test.
- **Fix the SC-5 test** to exercise the real path (stub vector table, not a vacuous monkeypatch) per QA-006.
- If the algebra shows 0.70 is unreachable without breaking SC-7, STOP and re-open the brief with Neal — that would mean SC-5 needs a Stage-2c-rescue disposition rule (e.g., embed-signal pairs route to SURFACE regardless of composite), which is a spec conversation, not a tuning knob.

## Also deferred (non-blocking, fold into the same fix commit or note in PR)

- CR-003 (WARNING): GraphEvidence double-queries DB; derive from B1/B6 raws per AC-23.
- QA-001/CR-004 (WARNING): `scripts/fetch_fasttext.py` URL 404s — verify a live URL + SHA before merging.
- CR-005 (WARNING): unused `datetime` import in scoring.py.

## Next-session sequence

1. Make the design decision above; implement on this branch; `pytest tests/ -x --tb=short` green including a real (non-vacuous) SC-5 test.
2. Optionally re-run only reviews (or just use `/code-review`) — do NOT relaunch the full rocket build; the build is done and sound apart from the weight design.
3. Flip queue row 8a BLOCKED → SHIPPED, PR per the template in HANDOFF_8a_NEXT_SESSION.md (note B3→8b deferral AND the weight-design amendment), `/review`, merge, delete BOTH handoff files in the merge commit.
4. Append CC-LEARNINGS entry for this run (new findings: review loop overran max-3-rounds contract — worth patching/filing upstream; mid-run infra commits on the run branch trigger reviewer FILE-PATHS blocking — commit infra elsewhere or pre-declare; vacuous monkeypatch let a core criterion false-pass 3 QA rounds; chmod-444 + atomic-rename pattern protected the live script).
5. Then feature 10 per FEATURE_QUEUE.md.

## Run forensics (committed on this branch)

- Verdicts/fix reports: `features/_logs/8a-*.md`, build diff `features/_logs/8a-build-diff.patch`
- Adversary debate: `features/_adversaries/8a-{design,skeptic,engineer,hardened}.md`
- Build prompt: `features/_prompts/8a.cc-prompt.md`
- Guard ledger: `features/REVIEW_OVERRIDES.md` — 8 reviewer-mutation reverts this run (reviewers still not read-only; upstream issue stands)
- Console log: `/tmp/rocket-8a-v2.log` (not committed; /tmp)
