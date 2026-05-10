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
