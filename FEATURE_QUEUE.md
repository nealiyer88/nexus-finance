## Active Feature Queue

> Pipeline reads this table to determine build order. Status updated as features ship. Dependencies are strict — downstream features cannot start until all dependencies are SHIPPED.

> **Column ORDER is load-bearing — rocket.sh parses: `# | Feature | Brief | Depends On | Status`.** Do not reorder. Dependencies are a comma-separated list of feature IDs (ALL must be SHIPPED); the old `X or Y` and `ALL` syntaxes are not supported by upstream rocket.sh and have been flattened during migration.

| # | Feature | Brief | Depends On | Status |
|---|---------|-------|------------|--------|
| 1 | rules-file-population | features/infrastructure/rules-file-population.md | — | SHIPPED |
| 2 | canonical-schema | features/infrastructure/canonical-schema.md | 1 | SHIPPED |
| 3 | normalizer | features/pipeline/normalizer.md | 1, 2 | SHIPPED |
| 4 | connector-base | features/infrastructure/connector-base.md | 2, 3 | SHIPPED |
| 5 | qb-connector | features/connectors/qb-connector.md | 3, 4 | SHIPPED |
| 6 | ruddr-connector | features/connectors/ruddr-connector.md | 3, 4 | SHIPPED |
| 7 | deterministic-blocking | features/pipeline/deterministic-blocking.md | 4, 5, 6 | SHIPPED |
| 8 | pairwise-scoring | features/pipeline/pairwise-scoring.md | 7 | QUEUED |
| 9 | threshold-llm-fallback | features/pipeline/threshold-llm-fallback.md | 8 | QUEUED |
| 10 | resolution-graph-update | features/pipeline/resolution-graph-update.md | 9 | QUEUED |
| 11 | approval-queue | features/dashboard/approval-queue.md | 9, 10 | QUEUED |
| 12 | matcher-orchestrator | features/pipeline/matcher-orchestrator.md | 7, 8, 9, 10 | QUEUED |
| 13 | historical-cold-start | features/data/historical-cold-start.md | 11, 12 | QUEUED |
| 14 | overview-entity-browser | features/dashboard/overview-entity-browser.md | 10, 11 | QUEUED |
| 15 | ar-reconciliation | features/dashboard/ar-reconciliation.md | 12, 14 | QUEUED |
| 16 | connectors-audit-infra | features/infrastructure/connectors-audit-infra.md | 5, 6 | QUEUED |
| 17 | signup-onboarding | features/infrastructure/signup-onboarding.md | 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16 | QUEUED |

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
-->
