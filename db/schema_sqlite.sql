-- Nexus Finance SQLite Graph Store Schema (V1, single-tenant, DORMANT in V1)
-- Mirrors the canonical / alias / edge / system_reference shape of db/schema.sql.
-- Postgres remains the operational store; this file is loaded by
-- tests/test_fixture_loads.py and reserved for future on-disk graph use.

CREATE TABLE IF NOT EXISTS canonical_entities (
    canonical_id    TEXT PRIMARY KEY,
    tenant_id       TEXT,
    canonical_name  TEXT NOT NULL,
    entity_type     TEXT NOT NULL CHECK (entity_type IN ('client','vendor','project','pl_unit','cost_center','contract','person')),
    entity_category TEXT NOT NULL CHECK (entity_category IN ('organization','person')),
    confidence      REAL,
    match_pattern   TEXT,
    match_signals   TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS canonical_entities_type ON canonical_entities (entity_type);

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_id  TEXT NOT NULL REFERENCES canonical_entities(canonical_id) ON DELETE CASCADE,
    value         TEXT NOT NULL,
    source        TEXT NOT NULL,
    category      TEXT NOT NULL,
    confidence    REAL,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (canonical_id, value, source)
);
CREATE INDEX IF NOT EXISTS entity_aliases_value ON entity_aliases (value);

CREATE TABLE IF NOT EXISTS entity_edges (
    edge_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_node      TEXT NOT NULL REFERENCES canonical_entities(canonical_id) ON DELETE CASCADE,
    target_node      TEXT NOT NULL REFERENCES canonical_entities(canonical_id) ON DELETE CASCADE,
    relationship     TEXT NOT NULL,
    source_category  TEXT NOT NULL,
    target_category  TEXT NOT NULL,
    weight           REAL,
    approved_by      TEXT,
    approval_count   INTEGER NOT NULL DEFAULT 0,
    last_transaction TEXT,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS entity_edges_source ON entity_edges (source_node);
CREATE INDEX IF NOT EXISTS entity_edges_target ON entity_edges (target_node);

CREATE TABLE IF NOT EXISTS system_references (
    ref_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_id    TEXT NOT NULL REFERENCES canonical_entities(canonical_id) ON DELETE CASCADE,
    source          TEXT NOT NULL,
    category        TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    external_fields TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS system_references_canonical ON system_references (canonical_id);

-- Transactions (backs Signal B3 — amount co-occurrence). tenant_id is
-- nullable TEXT here, mirroring the canonical_entities single-tenant V1
-- pattern; Postgres declares it UUID NOT NULL REFERENCES tenants(id).
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
