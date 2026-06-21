-- Migration 002 (SQLite mirror): LLM Training Data
-- SQLite equivalent of `002_llm_training_data.sql`. Loaded by Stage 5
-- tests on top of `db/schema_sqlite.sql`. Postgres-side typing
-- (UUID, JSONB, TIMESTAMPTZ) collapses to TEXT under SQLite.
--
-- Append-only within tenant lifetime. DELETE permitted on tenant
-- offboarding for GDPR right-to-erasure.

CREATE TABLE IF NOT EXISTS llm_training_data (
    call_id           TEXT PRIMARY KEY,
    tenant_id         TEXT,
    category_pair     TEXT NOT NULL,
    redacted_prompt   TEXT NOT NULL,
    prompt_sha256     TEXT NOT NULL,
    llm_response_json TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS llm_training_data_tenant ON llm_training_data (tenant_id);
CREATE INDEX IF NOT EXISTS llm_training_data_category_pair ON llm_training_data (category_pair);
