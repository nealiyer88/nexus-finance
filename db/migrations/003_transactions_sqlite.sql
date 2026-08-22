-- Migration 003 (SQLite mirror): Transactions
-- SQLite equivalent of 003_transactions.sql. Loaded on top of
-- db/schema_sqlite.sql; applies idempotently via CREATE TABLE IF NOT
-- EXISTS.

CREATE TABLE IF NOT EXISTS transactions (
    txn_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id              TEXT,
    source                 TEXT NOT NULL,
    category               TEXT NOT NULL,
    external_source_id     TEXT NOT NULL,
    txn_type               TEXT NOT NULL,
    amount                 REAL NOT NULL,
    currency               TEXT NOT NULL DEFAULT 'USD',
    txn_date               TEXT NOT NULL,
    period                 TEXT NOT NULL,
    counterparty_source_id TEXT,
    canonical_id           TEXT REFERENCES canonical_entities(canonical_id),
    created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tenant_id, source, external_source_id)
);
CREATE INDEX IF NOT EXISTS transactions_counterparty ON transactions (tenant_id, source, counterparty_source_id, period);
CREATE INDEX IF NOT EXISTS transactions_canonical    ON transactions (tenant_id, canonical_id, period);
