-- Nexus Finance Postgres / Supabase Schema (V1)
-- Multi-tenant operational store. SQLite graph store lives in db/schema_sqlite.sql.
-- Migrations: db/migrations/

-- Tenants
CREATE TABLE IF NOT EXISTS tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Connector credentials (encrypted at rest, scoped per system category)
CREATE TABLE IF NOT EXISTS connectors (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id),
    provider     TEXT NOT NULL,                       -- 'quickbooks' | 'ruddr'
    category     TEXT NOT NULL,                       -- 'accounting' | 'psa'
    credentials  JSONB NOT NULL DEFAULT '{}',
    last_sync    TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, provider)
);

-- Canonical entities (master nodes)
CREATE TABLE IF NOT EXISTS canonical_entities (
    canonical_id    TEXT PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    canonical_name  TEXT NOT NULL,
    entity_type     TEXT NOT NULL CHECK (entity_type IN ('client','vendor','project','pl_unit','cost_center','contract','person')),
    entity_category TEXT NOT NULL CHECK (entity_category IN ('organization','person')),
    confidence      NUMERIC(3,2),
    match_pattern   TEXT,
    match_signals   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS canonical_entities_tenant ON canonical_entities (tenant_id);
CREATE INDEX IF NOT EXISTS canonical_entities_type   ON canonical_entities (entity_type);

-- Entity aliases (per-source alias values with confidence)
CREATE TABLE IF NOT EXISTS entity_aliases (
    alias_id      BIGSERIAL PRIMARY KEY,
    canonical_id  TEXT NOT NULL REFERENCES canonical_entities(canonical_id) ON DELETE CASCADE,
    value         TEXT NOT NULL,
    source        TEXT NOT NULL,
    category      TEXT NOT NULL,
    confidence    NUMERIC(3,2),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (canonical_id, value, source)
);
CREATE INDEX IF NOT EXISTS entity_aliases_value ON entity_aliases (value);

-- Entity edges (typed graph edges with category metadata)
CREATE TABLE IF NOT EXISTS entity_edges (
    edge_id          BIGSERIAL PRIMARY KEY,
    source_node      TEXT NOT NULL REFERENCES canonical_entities(canonical_id) ON DELETE CASCADE,
    target_node      TEXT NOT NULL REFERENCES canonical_entities(canonical_id) ON DELETE CASCADE,
    relationship     TEXT NOT NULL,
    source_category  TEXT NOT NULL,
    target_category  TEXT NOT NULL,
    weight           NUMERIC(4,3),
    approved_by      TEXT,
    approval_count   INTEGER NOT NULL DEFAULT 0,
    last_transaction TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS entity_edges_source ON entity_edges (source_node);
CREATE INDEX IF NOT EXISTS entity_edges_target ON entity_edges (target_node);

-- System references (link source-system IDs to canonical_id)
CREATE TABLE IF NOT EXISTS system_references (
    ref_id          BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    canonical_id    TEXT NOT NULL REFERENCES canonical_entities(canonical_id) ON DELETE CASCADE,
    source          TEXT NOT NULL,                   -- 'quickbooks' | 'ruddr'
    category        TEXT NOT NULL,                   -- 'accounting' | 'psa'
    external_id     TEXT NOT NULL,
    external_fields JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source, external_id)
);
CREATE INDEX IF NOT EXISTS system_references_canonical ON system_references (canonical_id);

-- Transactions (backs Signal B3 — amount co-occurrence). tenant_id
-- mirrors the canonical_entities pattern: UUID NOT NULL here, nullable
-- TEXT on the SQLite side.
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

-- Approval decisions (structured training data capture for V2+ fine-tuning)
CREATE TABLE IF NOT EXISTS approval_decisions (
    decision_id            BIGSERIAL PRIMARY KEY,
    tenant_id              UUID NOT NULL REFERENCES tenants(id),
    entity_pair_a          TEXT NOT NULL,
    entity_pair_b          TEXT NOT NULL,
    signal_breakdown       JSONB,
    graph_evidence         JSONB,
    category_pair          TEXT,
    disposition            TEXT NOT NULL CHECK (disposition IN ('approved','rejected','corrected')),
    reasoning_trace        TEXT,
    confidence_at_decision NUMERIC(3,2),
    decided_by             TEXT,
    decided_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS approval_decisions_tenant ON approval_decisions (tenant_id);
CREATE INDEX IF NOT EXISTS approval_decisions_pair   ON approval_decisions (entity_pair_a, entity_pair_b);
CREATE INDEX IF NOT EXISTS approval_decisions_signal_breakdown_gin
    ON approval_decisions USING GIN (signal_breakdown);

-- Audit log (append-only, tagged by system category)
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    actor_id    UUID,
    action      TEXT NOT NULL,
    resource    TEXT NOT NULL,
    resource_id TEXT,
    category    TEXT,
    diff        JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_log_tenant_created ON audit_log (tenant_id, created_at DESC);
