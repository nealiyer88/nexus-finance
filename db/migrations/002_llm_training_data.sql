-- Migration 002: LLM Training Data
-- Backs Stage 5 (LLM fallback) training-data capture from Day 1. Each row
-- records one redacted Claude API call plus the structured response —
-- raw material for V2+ fine-tuning per the V1 product spec.
--
-- Append-only WITHIN a tenant lifetime: rows are never UPDATEd in V1.
-- DELETE permitted on tenant offboarding for GDPR right-to-erasure
-- (rules section 10 append-only audit-log rule applies to `audit_log`,
-- not to this table — `llm_training_data` is a distinct training corpus
-- whose rows are customer-derived even after redaction).

BEGIN;

DROP TABLE IF EXISTS llm_training_data CASCADE;

CREATE TABLE llm_training_data (
    call_id           TEXT PRIMARY KEY,
    tenant_id         UUID REFERENCES tenants(id),
    category_pair     TEXT NOT NULL,
    redacted_prompt   TEXT NOT NULL,
    prompt_sha256     TEXT NOT NULL,
    llm_response_json JSONB NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX llm_training_data_tenant ON llm_training_data (tenant_id);
CREATE INDEX llm_training_data_category_pair ON llm_training_data (category_pair);

COMMIT;
