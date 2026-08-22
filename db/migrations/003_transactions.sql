-- Migration 003: Transactions
-- Backs Signal B3 (amount co-occurrence, core/matching/scoring.py). See
-- db/schema.sql for the canonical definition; this migration applies it
-- idempotently via CREATE TABLE IF NOT EXISTS (not the DROP ... CASCADE
-- pattern used by migration 002 — transactions is additive, not a
-- from-scratch training corpus).

BEGIN;

CREATE TABLE IF NOT EXISTS transactions (
    txn_id                 BIGSERIAL PRIMARY KEY,
    tenant_id              UUID NOT NULL REFERENCES tenants(id),
    source                 TEXT NOT NULL,
    category               TEXT NOT NULL,
    external_source_id     TEXT NOT NULL,
    txn_type               TEXT NOT NULL,
    amount                 NUMERIC(18,2) NOT NULL,
    currency               TEXT NOT NULL DEFAULT 'USD',
    txn_date               DATE NOT NULL,
    period                 TEXT NOT NULL,
    counterparty_source_id TEXT,
    canonical_id           TEXT REFERENCES canonical_entities(canonical_id),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source, external_source_id)
);
CREATE INDEX IF NOT EXISTS transactions_counterparty ON transactions (tenant_id, source, counterparty_source_id, period);
CREATE INDEX IF NOT EXISTS transactions_canonical    ON transactions (tenant_id, canonical_id, period);

COMMIT;
