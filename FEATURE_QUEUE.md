## Active Feature Queue

> Pipeline reads this table to determine build order. Status updated as features ship. Dependencies are strict — downstream features cannot start until all dependencies are SHIPPED.

> **Column ORDER is load-bearing — rocket.sh parses: `# | Feature | Brief | Depends On | Spec | Status`.** Do not reorder. Dependencies are a comma-separated list of feature IDs (ALL must be SHIPPED); the old `X or Y` and `ALL` syntaxes are not supported by upstream rocket.sh and have been flattened during migration.
>
> **Spec column** points each row at the product-spec version the brief was authored against (e.g. `v3`, `v4`, `v4 §5,9,17` when a specific section is load-bearing). Features SHIPPED under an earlier spec carry that spec's tag — retrofit rows (e.g. 8a) update them. This makes every row auditable against the canonical spec at any moment.

| # | Feature | Brief | Depends On | Spec | Status |
|---|---------|-------|------------|------|--------|
| 1 | rules-file-population | features/infrastructure/rules-file-population.md | — | v3 | SHIPPED |
| 2 | canonical-schema | features/infrastructure/canonical-schema.md | 1 | v3 | SHIPPED |
| 3 | normalizer | features/pipeline/normalizer.md | 1, 2 | v3 | SHIPPED |
| 4 | connector-base | features/infrastructure/connector-base.md | 2, 3 | v3 | SHIPPED |
| 5 | qb-connector | features/connectors/qb-connector.md | 3, 4 | v3 | SHIPPED |
| 6 | ruddr-connector | features/connectors/ruddr-connector.md | 3, 4 | v3 | SHIPPED |
| 7 | deterministic-blocking | features/pipeline/deterministic-blocking.md | 4, 5, 6 | v3 (retrofit by 8a) | SHIPPED |
| 8 | pairwise-scoring | features/pipeline/pairwise-scoring.md | 7 | v3 (retrofit by 8a) | QUEUED |
| 8a | fasttext-signal-retrofit | features/pipeline/fasttext-signal-retrofit.md | 7, 8 | v4 §5,9,17 | QUEUED |
| 9 | threshold-llm-fallback | features/pipeline/threshold-llm-fallback.md | 8 | v3 (unaffected by v4) | QUEUED |
| 10 | resolution-graph-update | features/pipeline/resolution-graph-update.md | 9 | v4 | QUEUED |
| 11 | approval-queue | features/dashboard/approval-queue.md | 9, 10 | v4 | QUEUED |
| 12 | matcher-orchestrator | features/pipeline/matcher-orchestrator.md | 7, 8, 8a, 9, 10 | v4 §7 | QUEUED |
| 13 | historical-cold-start | features/data/historical-cold-start.md | 11, 12 | v4 | QUEUED |
| 14 | overview-entity-browser | features/dashboard/overview-entity-browser.md | 10, 11 | v4 | QUEUED |
| 15 | ar-reconciliation | features/dashboard/ar-reconciliation.md | 12, 14 | v4 | QUEUED |
| 16 | connectors-audit-infra | features/infrastructure/connectors-audit-infra.md | 5, 6 | v4 | QUEUED |
| 17 | signup-onboarding | features/infrastructure/signup-onboarding.md | 1, 2, 3, 4, 5, 6, 7, 8, 8a, 9, 10, 11, 12, 13, 14, 15, 16 | v4 | QUEUED |

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

> **Spec column added (2026-06-20):** rocket.sh parser updated in the same commit
> to read 6 columns instead of 5. The `Spec` field is audit metadata only — the
> parser does not gate on it. Feature 12's `Depends On` updated to include 8a
> explicitly (v4 retrofit note flagged this dependency).

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
