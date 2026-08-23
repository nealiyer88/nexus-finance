-- Migration 004 (SQLite-only): Pending Decisions
-- Stage 4 -> approval-queue handoff (feature 10b). This table has no
-- counterpart in the other engine's schema file — it is SQLite-only,
-- exactly like `llm_training_data` (002_llm_training_data_sqlite.sql),
-- whose GDPR carve-out this table's rows follow: append-only within
-- tenant lifetime, DELETE permitted on tenant offboarding for GDPR
-- right-to-erasure.
--
-- No foreign key on `top_canonical_id` — the graph tables cascade on
-- delete, but a pending row is decision history and must not vanish
-- because a candidate canonical was later deleted. Deliberate divergence
-- from the graph tables' FK style.

CREATE TABLE IF NOT EXISTS pending_decisions (
    pending_id           TEXT PRIMARY KEY,
    tenant_id             TEXT,
    decision_key          TEXT NOT NULL UNIQUE,
    status                TEXT NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending', 'approved', 'rejected', 'corrected')),
    source_entity_id      TEXT NOT NULL,
    action                TEXT NOT NULL,
    top_canonical_id      TEXT,
    top_score             REAL,
    category_pair         TEXT NOT NULL,
    cluster_conflict      INTEGER NOT NULL DEFAULT 0,
    abbreviation_rescue    INTEGER NOT NULL DEFAULT 0,
    llm_call_id           TEXT,
    entity_json           TEXT NOT NULL,
    disposition_json      TEXT NOT NULL,
    proposal_json         TEXT NOT NULL,
    created_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at           TEXT,
    resolved_by           TEXT,
    outcome_canonical_id  TEXT
);
CREATE INDEX IF NOT EXISTS pending_decisions_tenant_status ON pending_decisions (tenant_id, status);
CREATE INDEX IF NOT EXISTS pending_decisions_status_score ON pending_decisions (status, top_score);
