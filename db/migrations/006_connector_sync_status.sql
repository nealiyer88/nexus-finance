-- Feature 16: connector sync-status columns.
-- Adds two nullable columns to `connectors` for the last manual-sync
-- outcome. No SQLite mirror — `connectors` is Postgres-only (Owner
-- Decision 4 / features/infrastructure/connectors-audit-infra.md).
ALTER TABLE connectors ADD COLUMN IF NOT EXISTS last_sync_status TEXT;
ALTER TABLE connectors ADD COLUMN IF NOT EXISTS last_sync_error TEXT;
