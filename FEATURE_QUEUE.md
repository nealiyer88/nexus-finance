## Active Feature Queue

> Pipeline reads this table to determine build order. Status updated as features ship. Dependencies are strict — downstream features cannot start until all dependencies are SHIPPED.

> **Column ORDER is load-bearing — rocket.sh parses: `# | Feature | Brief | Depends On | Status | Spec`.** Do not reorder. Dependencies are a comma-separated list of feature IDs (ALL must be SHIPPED); the old `X or Y` and `ALL` syntaxes are not supported by upstream rocket.sh and have been flattened during migration.
>
> **Status MUST be the 6th column and Spec MUST come after it.** Upstream rocket.sh
> hard-codes the position: `get_next_feature`, `feature_deps`, the co-scheduling
> scan, and `_promote_verify_row` all read `$6` as Status and stop there. Spec sitting
> at `$6` makes every row read as status `v4 …` instead of `QUEUED`, so NO feature is
> ever eligible and the loop exits reporting "0 features processed" with no error —
> while the SHIPPED/QUEUED tallies still look correct, because those grep the whole
> line. Do not "fix" this by patching rocket.sh: that patch existed before
> 2026-08-11, was silently dropped by a harness upgrade, and cost a run. Anything
> after Status is ignored in feature mode, which is why Spec is safe there.
>
> **Spec column** points each row at the product-spec version the brief was authored against (e.g. `v3`, `v4`, `v4 §5,9,17` when a specific section is load-bearing). Features SHIPPED under an earlier spec carry that spec's tag — retrofit rows (e.g. 8a) update them. This makes every row auditable against the canonical spec at any moment.

| # | Feature | Brief | Depends On | Status | Spec |
|---|---------|-------|------------|--------|------|
| 1 | rules-file-population | features/infrastructure/rules-file-population.md | — | SHIPPED | v3 |
| 2 | canonical-schema | features/infrastructure/canonical-schema.md | 1 | SHIPPED | v3 |
| 3 | normalizer | features/pipeline/normalizer.md | 1, 2 | SHIPPED | v3 |
| 4 | connector-base | features/infrastructure/connector-base.md | 2, 3 | SHIPPED | v3 |
| 5 | qb-connector | features/connectors/qb-connector.md | 3, 4 | SHIPPED | v3 |
| 6 | ruddr-connector | features/connectors/ruddr-connector.md | 3, 4 | SHIPPED | v3 |
| 7 | deterministic-blocking | features/pipeline/deterministic-blocking.md | 4, 5, 6 | SHIPPED | v3 (retrofit by 8a) |
| 8 | pairwise-scoring | features/pipeline/pairwise-scoring.md | 7 | SHIPPED | v3 (retrofit by 8a) |
| 8a | fasttext-signal-retrofit | features/pipeline/fasttext-signal-retrofit.md | 7, 8 | SHIPPED | v4 §5,9,17 |
| 8b | b3-transactions-table-and-amount-signal | features/pipeline/b3-transactions-table-and-amount-signal.md | 8a | BLOCKED | v4 §9 B3 |
| 9 | threshold-llm-fallback | features/pipeline/threshold-llm-fallback.md | 8 | SHIPPED | v3 (unaffected by v4) |
| 10 | resolution-graph-update | features/pipeline/resolution-graph-update.md | 9 | BLOCKED | v4 |
| 11 | approval-queue | features/dashboard/approval-queue.md | 9, 10 | QUEUED | v4 |
| 12 | matcher-orchestrator | features/pipeline/matcher-orchestrator.md | 7, 8, 8a, 8b, 9, 10 | QUEUED | v4 §7 |
| 13 | historical-cold-start | features/data/historical-cold-start.md | 11, 12 | QUEUED | v4 |
| 14 | overview-entity-browser | features/dashboard/overview-entity-browser.md | 10, 11 | QUEUED | v4 |
| 15 | ar-reconciliation | features/dashboard/ar-reconciliation.md | 12, 14 | QUEUED | v4 |
| 16 | connectors-audit-infra | features/infrastructure/connectors-audit-infra.md | 5, 6 | BLOCKED | v4 |
| 17 | signup-onboarding | features/infrastructure/signup-onboarding.md | 1, 2, 3, 4, 5, 6, 7, 8, 8a, 9, 10, 11, 12, 13, 14, 15, 16 | QUEUED | v4 |

> **v4 retrofit note (2026-06-20):** Product spec v4 made pre-trained fastText
> V1-mandatory (Stage 2c blocking + Stage 3 Signal Set C) and raised the Phase 1
> auto-match gate from 90% → 95%. Features 7 and 8 shipped under the V3 rules file
> (fastText NOT-SCOPE, n-gram Jaccard as bridge signal) and are therefore INCOMPLETE
> against v4. Feature 8a is the reconciliation. It is order-independent from feature 9
> but is a HARD dependency for feature 12 (matcher-orchestrator) — do not run 12, and
> do not measure against the 95% gate, until 8a is SHIPPED.
>
> Rules file `.claude/rules/01-nexus-finance-v1.md` updated (§1, §6, §11, §13):
> pre-trained fastText moved to IN-SCOPE; fine-tuned fastText remains NOT-SCOPE.
> Phase 1 success gate (90% → 95%) still needs updating in TEMPLATE.md, roadmap.md,
> and the Phase-1 success criteria of the pipeline feature briefs.

> **Spec column added (2026-06-20):** rocket.sh was patched in the same commit to
> read 6 columns instead of 5, with Spec BEFORE Status. The `Spec` field is audit
> metadata only — the parser does not gate on it. Feature 12's `Depends On` updated
> to include 8a explicitly (v4 retrofit note flagged this dependency).
>
> **Superseded (2026-08-22):** that rocket.sh patch was the only local change to the
> file, and the 2026-08-11 harness upgrade replaced rocket.sh wholesale — dropping it.
> The next run selected nothing and exited "0 features processed" with no error.
> Fixed by moving Spec AFTER Status so stock upstream rocket.sh parses the table
> unmodified. The harness is now un-patched and upgrades cleanly; the column order
> is what carries the compatibility.

<!--
Migration notes (2026-06-20):

Format changed from the legacy nexus-finance schema (# | Brief | Depends On | Complexity | Status)
to the upstream rocket-loop schema (# | Feature | Brief | Depends On | Status). The Complexity
column was dropped (not used by the parser). A Feature short-name column was added (extracted
from each brief's filename slug).

Dependency syntax migrations:
- Row 7  "4, 5 or 6"  →  "4, 5, 6"   (both connectors are SHIPPED; stricter, factually correct)
- Row 17 "ALL"        →  "1..16"     (expanded explicitly; new parser has no ALL shorthand)

Feature 7 (deterministic-blocking) status verified SHIPPED from prior state on main.
Statuses for 8 and 9 reflect what is on main at the time of this migration — feature/pairwise-scoring
and feature/threshold-llm-fallback exist as branches on origin but neither is merged to main yet.

Spec column added 2026-06-20. rocket.sh parser updated to read 6 columns. Spec values
populated from each brief's Spec section ref where authored; rows pre-dating an explicit
spec ref were tagged with their shipping spec version (v3 for features 1–9). 8a is the only
explicit v4-mandatory row; rows 10–17 are tagged v4 because they remain unbuilt and would
be authored against v4 going forward. 12's depends list updated to include 8a explicitly.
-->
