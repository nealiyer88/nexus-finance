## Active Feature Queue

| # | Brief | Depends On | Complexity | Status |
|---|-------|------------|------------|--------|
| 1 | features/infrastructure/rules-file-population.md | — | S | SHIPPED |
| 2 | features/infrastructure/canonical-schema.md | 1 | M | SHIPPED |
| 3 | features/pipeline/normalizer.md | 1, 2 | M | SHIPPED |
| 4 | features/infrastructure/connector-base.md | 2, 3 | S | QUEUED |
| 5 | features/connectors/qb-connector.md | 3, 4 | L | QUEUED |
| 6 | features/connectors/ruddr-connector.md | 3, 4 | L | QUEUED |
| 7 | features/pipeline/deterministic-blocking.md | 4, 5 or 6 | L | QUEUED |
| 8 | features/pipeline/pairwise-scoring.md | 7 | L | QUEUED |
| 9 | features/pipeline/threshold-llm-fallback.md | 8 | M | QUEUED |
| 10 | features/pipeline/resolution-graph-update.md | 9 | M | QUEUED |
| 11 | features/dashboard/approval-queue.md | 9, 10 | M | QUEUED |
| 12 | features/pipeline/matcher-orchestrator.md | 7, 8, 9, 10 | M | QUEUED |
| 13 | features/data/historical-cold-start.md | 11, 12 | M | QUEUED |
| 14 | features/dashboard/overview-entity-browser.md | 10, 11 | M | QUEUED |
| 15 | features/dashboard/ar-reconciliation.md | 12, 14 | M | QUEUED |
| 16 | features/infrastructure/connectors-audit-infra.md | 5, 6 | M | QUEUED |
| 17 | features/infrastructure/signup-onboarding.md | ALL | L | QUEUED |
