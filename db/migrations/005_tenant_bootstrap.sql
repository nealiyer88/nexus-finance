-- Seeds the fixed bootstrap tenant row (core.graph.pg.BOOTSTRAP_TENANT_ID)
-- so writers with no caller-supplied tenant have a valid FK target.
-- Idempotent: safe to re-run against a database that already has the row.

INSERT INTO tenants (id, name, slug)
VALUES ('00000000-0000-0000-0000-000000000000', 'Bootstrap Tenant', 'bootstrap')
ON CONFLICT (id) DO NOTHING;
