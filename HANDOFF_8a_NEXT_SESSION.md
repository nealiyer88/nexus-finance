# Handoff Prompt — 8a Recovery (Option D, next session)

> This file is intentionally not gitignored. After the next session's work ships, **delete this file in the same commit that merges 8a**.

---

## Paste this into a fresh Claude Code session

```
I'm resuming work on the Nexus Finance project (repo: /Users/nealiyer/code/nexus-finance, GitHub: nealiyer88/nexus-finance). Read HANDOFF_8a_NEXT_SESSION.md in full before doing anything else. Then read .claude/rules/01-nexus-finance-v1.md, CLAUDE.md, and CC-LEARNINGS.md (last entries dated 2026-06-21 cover the deadlock you're recovering from). Then execute Option D below.

You DO NOT need to re-read every file in the repo. The handoff doc has the precise paths.
```

---

## Where we are

- Branch: `main` (clean, at `09d2dd5` after PR #9 merged CC-LEARNINGS)
- Recovery branch: `origin/feature/8a-blocked` — has the BLOCKED 8a build for forensics
- Local feature 8a NEVER merged. Queue row 8a still says QUEUED (the sed bug in rocket.sh's BLOCKED-flip silently failed — see CC-LEARNINGS 2026-06-21 "rocket.sh's queue status flip uses sed -i syntax that breaks on macOS BSD sed")
- Features shipped via the Rocket loop so far: 1–9 inclusive (8 and 9 just landed in PRs #7 and earlier)
- Features QUEUED: 8a, 10, 11, 12, 13, 14, 15, 16, 17

## What went wrong (one-paragraph version)

The rocket loop deadlocked on feature 8a (fastText signal retrofit). The original brief at `features/pipeline/fasttext-signal-retrofit.md` mandated all six Signal Set B signals (B1–B6). The Phase 1 hardened design / build prompt cut B2–B5 entirely, claiming they need a transactions table not in V1. Phase 4 QA reviewed against the BRIEF (mandate of 6 signals), failed the build for having only 2. Phase 5 fixer added scaffold fields for B2–B5 to satisfy QA. Phase 4 code reviewer caught the scaffolds violated the build prompt's "no placeholders" NON-GOAL and BLOCKED. Loop hit max 3 fix iterations and gave up. Cost: $15.25. Audit: 10 reviewer-mutation-revert events in `features/REVIEW_OVERRIDES.md` (on the recovery branch).

## What the user actually wants — and why Option D is right

The v4 spec promoted Signal Set B to the **moat** (the layer that proves cross-category resolution is more than fuzzy string matching). Shipping only B1+B6 ships the opposite of the moat. The user explicitly directed: **"heed the spec; it's literally the moat of our product."**

The brief was internally inconsistent. Reality:

| Signal | Data required | In V1 schema? |
|---|---|---|
| B1 shared person | canonical_entities + entity_edges | ✅ already shipped |
| **B2 project-code fragment in QB ref/class/memo** | `system_references.external_fields` | ✅ exists today |
| B3 amount co-occurrence within AMOUNT_TOLERANCE same period | a transactions table | ❌ does NOT exist |
| **B4 shared email domain** | `system_references.external_fields.email` | ✅ exists today |
| **B5 temporal co-occurrence, 30-day first-seen window** | `canonical_entities.created_at` | ✅ exists today |
| B6 graph neighborhood overlap | entity_edges | ✅ already shipped |

**Only B3 actually requires new infrastructure.** B2, B4, B5 are buildable today. The build prompt was wrong to lump them with B3.

## Option D — what to execute

### Phase 0: Set up safely

```bash
cd /Users/nealiyer/code/nexus-finance
git checkout main && git pull origin main

# CRITICAL: do NOT launch rocket.sh from main again.
# Create a dedicated branch for this run:
git checkout -b rocket-run-8a-v2

# Verify the previous BLOCKED branch is on origin (for reference, not to merge):
git ls-remote origin feature/8a-blocked
```

### Phase 1: Update the brief

Edit `features/pipeline/fasttext-signal-retrofit.md`. Specifically:

1. **Keep** the In Scope items for: fastText vector loader (`embeddings.py`), Stage 2c blocking wire-in, Stage 3 Signal C cosine.
2. **Replace** the Signal Set B reconciliation bullet with:

   > **Stage 3 Signal Set B reconciliation** (`core/matching/scoring.py`): implement five of six signals using existing V1 schema. Each signal gated to the 0.70–0.90 ambiguous zone, with v4 boost ranges enforced and the +0.20 hard cap applied AFTER summation:
   >
   > - B1 shared person entity (+0.05–0.10) — reads `entity_edges` SAME_AS/MEMBER_OF
   > - B2 project-code fragment in QB ref/class/memo (+0.08–0.12) — reads `system_references.external_fields` JSON for `class`/`memo` fields, extracts segments via the existing project-code shape utility, matches against PSA-side project codes via `_check_psa_abbreviation`'s shortcode logic
   > - B4 shared email domain (+0.05–0.08) — reads `system_references.external_fields.email`, splits on `@`, compares domain (case-insensitive)
   > - B5 temporal co-occurrence, same 30-day first-seen window (+0.03–0.05) — reads `canonical_entities.created_at` on both canonicals, fires when `abs(delta_days) <= 30`
   > - B6 graph neighborhood overlap (+0.02–0.05 per shared node, capped +0.10) — already shipped, reconcile to spec-mandated cap
   >
   > **Every applied boost logged in `signal_breakdown`** with signal id, raw value, applied value (per spec v4 §9 audit). Total Signal Set B boost hard-capped at +0.20.

3. **Add a new In Scope item:**

   > **Defensive guard against deferred-signal accidental population:** B3 (amount co-occurrence) is OUT OF SCOPE for this feature — it requires a transactions table not in V1 schema. `_compute_b_boosts` MUST NOT include any field, parameter, or branch related to B3. Reviewers MUST grep the diff for `amount_cooccurrence`, `transaction`, and similar; their presence in any code or test is a BLOCKING violation. (This guard exists because the prior attempt at this feature shipped no-op B3 scaffolds that violated NON-GOALS — see CC-LEARNINGS 2026-06-21.)

4. **Replace** the Out of Scope section's B3 bullet with:

   > B3 amount co-occurrence — deferred to **feature 8b** (`features/pipeline/b3-transactions-table-and-amount-signal.md`, to be authored). 8b will add a minimal transactions table to `db/schema.sql` and `db/schema_sqlite.sql` plus the B3 signal implementation. 8b is the prerequisite for feature 12 (matcher-orchestrator) to measure against the 95% gate over real cycles.

5. **Update Success Criteria** to enumerate the 5 signals shipped here. Replace the prior "Signal Set B: all of B1–B6 present and reachable" with "B1, B2, B4, B5, B6 present and reachable; B3 explicitly absent from `_compute_b_boosts`; synthetic pair tripping ≥4 signals receives total B boost exactly capped at +0.20 with each itemized in signal_breakdown."

6. **Update Dependencies** to note: "Feature 8b (B3 + transactions table) will follow but does NOT gate this feature."

### Phase 2: Stub the 8b brief

Create `features/pipeline/b3-transactions-table-and-amount-signal.md` as a short brief (one page). It records:

- Why 8b exists (B3 unbuildable in V1 without a transactions table)
- What 8b ships (minimal transactions schema + B3 signal implementation)
- Dependency: 8a SHIPPED
- Estimated complexity: M

Also add row 8b to `FEATURE_QUEUE.md` right after 8a:

```
| 8b | b3-transactions-table-and-amount-signal | features/pipeline/b3-transactions-table-and-amount-signal.md | 8a | v4 §9 B3 | QUEUED |
```

Update row 12 (`matcher-orchestrator`) Depends On `7, 8, 8a, 9, 10` → `7, 8, 8a, 8b, 9, 10`. (95% gate requires B3.)

### Phase 3: Launch rocket.sh

```bash
# Already on rocket-run-8a-v2 from Phase 0.
bash rocket.sh --feature 8a 2>&1 | tee /tmp/rocket-8a-v2.log
```

In a separate terminal:

```bash
tail -f features/ROCKET_LIVE.md
```

Expected wall time: ~1.5 h. Expected cost: ~$15 (similar to prior run).

### Phase 4: PR + /review + merge

Once rocket.sh ships (exit 0, queue row 8a SHIPPED, branch pushed to origin):

```bash
# Open PR from the rocket-run-8a-v2 branch:
gh pr create --base main --head rocket-run-8a-v2 \
  --title "Ship 8a fasttext-signal-retrofit (Option D: 5 of 6 B-signals, B3 deferred to 8b)" \
  --body "<see template at bottom of this handoff>"

# Run /review on the PR:
/review <PR number>

# If review passes:
gh pr merge <PR number> --merge --delete-branch

# Verify queue row 8a SHIPPED on main:
git checkout main && git pull
grep -E "^\| 8a " FEATURE_QUEUE.md
```

### Phase 5: Cleanup

Delete this handoff file:

```bash
git rm HANDOFF_8a_NEXT_SESSION.md
git commit -m "Remove 8a recovery handoff (8a shipped via PR #X)"
git push
```

Mark feature 8a recovery in CC-LEARNINGS only if something NEW was learned this run.

## Safeguards to apply during the run

These come straight from the CC-LEARNINGS 2026-06-21 entries — apply proactively:

1. **NEVER launch `bash rocket.sh` from `main`.** Always from a dedicated branch (you're doing this — `rocket-run-8a-v2`).
2. **Monitor `features/REVIEW_OVERRIDES.md`** — if more than ~3 guard-revert events fire, the reviewer agents are misbehaving badly enough that the run may not converge. Consider killing and investigating.
3. **If the queue-flip sed bug fires again**, manually edit `FEATURE_QUEUE.md` after rocket exits.
4. **If the deadlock pattern recurs** (QA says "need more signals", code review says "no scaffolds"), STOP. The brief is still inconsistent with the build prompt. Don't loop further; ask the user.

## PR template for the eventual 8a ship

```
## Summary
Ships feature 8a (`fasttext-signal-retrofit`) under Option D: implements 5 of 6 Signal Set B signals (B1, B2, B4, B5, B6) + Signal Set C (fastText cosine) + Stage 2c blocking integration. B3 explicitly deferred to feature 8b which adds the transactions table prerequisite.

This is the recovery path after the 2026-06-20→21 attempt BLOCKED on a brief vs build-prompt deadlock (see CC-LEARNINGS 2026-06-21, recovery branch at origin/feature/8a-blocked).

## What ships (vs the failed attempt)
- `core/matching/embeddings.py` — fastText vector loader with pure-Python load path (no C++ compile)
- `core/matching/indices.py` — EmbeddingIndex class for Stage 2c top-k cosine retrieval
- `core/matching/blocking.py` — Stage 2c wire-in: fastText candidates union with token+trigram candidates
- `core/matching/scoring.py` — Signal Set C (fastText cosine) weighted into the ensemble; Signal Set B with 5 of 6 signals, +0.20 hard cap, every boost itemized in signal_breakdown
- `core/matching/weights.py` — fasttext_cosine field with category-pair dispatch (PSA↔Accounting ≠ Accounting↔Accounting)
- `core/matching/types.py` — SignalBoost class for audit logging; GraphEvidence extended with B2/B4/B5 fields (computed, NOT scaffold)
- `scripts/fetch_fasttext.py` — idempotent model fetch
- `tests/test_embeddings.py`, `tests/test_blocking.py`, `tests/test_scoring.py` extended

## What does NOT ship
- B3 amount co-occurrence — deferred to feature 8b (needs transactions table, not in V1 schema)
- `_compute_b_boosts` does NOT include `amount_cooccurrence_bonus` (defensive guard against the prior attempt's scaffold violation)

## Tests
- Full suite green
- Specifically: synthetic pair tripping ≥4 B-signals receives total B boost exactly capped at +0.20 with each boost itemized

## Spec compliance
- v4 §5: fastText as the 80→95 bridge ✓
- v4 §9: Stage 2c blocking + Signal Sets A/B/C ✓ (B3 deferred to 8b)
- v4 §17: V1 Build Scope (pre-trained fastText IN, fine-tuned OUT) ✓
- Signal Set B audit logging per v4 §9 ✓

## After merge
- FEATURE_QUEUE.md row 8a → SHIPPED
- HANDOFF_8a_NEXT_SESSION.md deleted in this commit
- Feature 8b takes over for B3
```

## Token budget if context tight

If the next session is also tight on context, you can skip these reads:
- The recovery branch's `features/_logs/8a-*-verdict.md` (long, only useful for understanding the prior deadlock — already summarized here)
- Old CC-LEARNINGS entries before 2026-06-21
- `RUN_LOG.md` entries before 2026-06-21

You DO need to read:
- This handoff file
- `.claude/rules/01-nexus-finance-v1.md` (V1 architectural guardrails)
- `CLAUDE.md`
- `features/pipeline/fasttext-signal-retrofit.md` (the brief — you'll rewrite it)
- CC-LEARNINGS.md last 8 entries (2026-06-21 cluster — the deadlock postmortem)
