-- Migration 001: Canonical Entity Schema
-- Replaces the pre-v3 generic entities/approvals tables with canonical
-- entity, alias, edge, system_reference, approval_decisions, and audit_log
-- tables aligned with the V1 product spec.

BEGIN;

-- Drop pre-v3 tables (entities, approvals) if they exist.
DROP TABLE IF EXISTS approvals CASCADE;
DROP TABLE IF EXISTS entities  CASCADE;

-- Drop any prior canonical tables before re-creating (idempotent migration).
DROP TABLE IF EXISTS approval_decisions CASCADE;
DROP TABLE IF EXISTS system_references  CASCADE;
DROP TABLE IF EXISTS entity_edges        CASCADE;
DROP TABLE IF EXISTS entity_aliases      CASCADE;
DROP TABLE IF EXISTS canonical_entities  CASCADE;
DROP TABLE IF EXISTS audit_log           CASCADE;

-- Tenants and connectors are preserved across the migration (kept from prior schema).
CREATE TABLE IF NOT EXISTS tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS connectors (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id),
    provider     TEXT NOT NULL,
    category     TEXT NOT NULL,
    credentials  JSONB NOT NULL DEFAULT '{}',
    last_sync    TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, provider)
);

CREATE TABLE canonical_entities (
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
CREATE INDEX canonical_entities_tenant ON canonical_entities (tenant_id);
CREATE INDEX canonical_entities_type   ON canonical_entities (entity_type);

CREATE TABLE entity_aliases (
    alias_id      BIGSERIAL PRIMARY KEY,
    canonical_id  TEXT NOT NULL REFERENCES canonical_entities(canonical_id) ON DELETE CASCADE,
    value         TEXT NOT NULL,
    source        TEXT NOT NULL,
    category      TEXT NOT NULL,
    confidence    NUMERIC(3,2),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (canonical_id, value, source)
);
CREATE INDEX entity_aliases_value ON entity_aliases (value);

CREATE TABLE entity_edges (
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
CREATE INDEX entity_edges_source ON entity_edges (source_node);
CREATE INDEX entity_edges_target ON entity_edges (target_node);

CREATE TABLE system_references (
    ref_id          BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    canonical_id    TEXT NOT NULL REFERENCES canonical_entities(canonical_id) ON DELETE CASCADE,
    source          TEXT NOT NULL,
    category        TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    external_fields JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source, external_id)
);
CREATE INDEX system_references_canonical ON system_references (canonical_id);

CREATE TABLE approval_decisions (
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
CREATE INDEX approval_decisions_tenant ON approval_decisions (tenant_id);
CREATE INDEX approval_decisions_pair   ON approval_decisions (entity_pair_a, entity_pair_b);
CREATE INDEX approval_decisions_signal_breakdown_gin
    ON approval_decisions USING GIN (signal_breakdown);

CREATE TABLE audit_log (
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
CREATE INDEX audit_log_tenant_created ON audit_log (tenant_id, created_at DESC);

COMMIT;
