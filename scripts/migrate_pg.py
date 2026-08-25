"""Forward-only Postgres migration runner.

DATA LOSS WARNING: some migrations applied by this runner open with a
block of unconditional DROP TABLE statements. Running a destructive
migration by hand, or deleting its `schema_migrations` row and re-running
it, destroys all data in the tables that migration drops. There is no
escape hatch and no automatic snapshot for this data loss.

The following migrations (derived by grepping `db/migrations/*.sql`,
excluding `*_sqlite.sql`, for a line starting with `DROP TABLE`) contain
such a block:
  - 001_canonical_schema.sql
  - 002_llm_training_data.sql

Usage:
    .venv/bin/python scripts/migrate_pg.py [--dry-run]

Selection rule: this runner applies exactly the filenames listed in
`db/migrations/postgres.manifest`, in the order they appear there, and
never reads or applies any other file in `db/migrations/`. If the
manifest names a file absent from disk, the runner exits non-zero, naming
the missing filename, before applying anything and before opening any
connection.

`DATABASE_URL` is never printed by this script, on any path, including
failure paths.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

from core.graph import pg

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
MANIFEST_PATH = MIGRATIONS_DIR / "postgres.manifest"

# A migration is DESTRUCTIVE when it contains an unconditional `DROP TABLE`
# statement. The set is derived by scanning the migration text at the moment
# of use — never from a hardcoded filename list. `IF EXISTS` only suppresses
# the error when the table is absent; it does not make the DROP conditional.
_DROP_TABLE_RE = re.compile(
    r"^\s*DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([^\s;]+)",
    re.IGNORECASE | re.MULTILINE,
)


def parse_manifest(path: Path) -> list[str]:
    """Parse a `postgres.manifest` file into an ordered filename list.

    Blank lines and `#`-comment lines are ignored. Order is preserved.
    """
    filenames = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        filenames.append(stripped)
    return filenames


def dropped_tables(sql: str) -> list[str]:
    """Return the table names an unconditional `DROP TABLE` statement drops.

    Table names are enumerated by grep at the moment of use, so no migration
    filename, prefix, or table name is ever hardcoded here.
    """
    names: list[str] = []
    for match in _DROP_TABLE_RE.finditer(sql):
        raw = match.group(1).strip().strip('"')
        # Strip any schema qualifier and surrounding quoting.
        name = raw.split(".")[-1].strip('"')
        if name and name not in names:
            names.append(name)
    return names


def is_destructive_migration(path: Path) -> bool:
    """True when the migration file contains an unconditional `DROP TABLE`."""
    return bool(dropped_tables(path.read_text()))


def _table_exists(conn, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name = %s)",
            (table_name,),
        )
        row = cur.fetchone()
        return bool(row[0]) if row else False


def _migration_applied(conn, filename: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM schema_migrations WHERE filename = %s", (filename,))
        return cur.fetchone() is not None


def run(dry_run: bool = False) -> int:
    """Apply pending manifested migrations. Returns a process exit code."""
    filenames = parse_manifest(MANIFEST_PATH)

    resolved: list[Path] = []
    for filename in filenames:
        path = MIGRATIONS_DIR / filename
        if not path.is_file():
            print(f"manifest names missing file: {filename}")
            return 1
        resolved.append(path)

    if dry_run:
        print("plan:")
        for filename in filenames:
            print(f"  {filename}")
        return 0

    conn = pg.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    filename TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        conn.commit()

        for filename, path in zip(filenames, resolved):
            if _migration_applied(conn, filename):
                print(f"skipped {filename}")
                continue

            sql = path.read_text()

            # DESTRUCTIVE-FILE GUARD. This file is not recorded in
            # schema_migrations (checked above). If it drops tables that
            # already exist, the database was created some other way and
            # applying this file would silently destroy data.
            existing_targets = [
                table for table in dropped_tables(sql) if _table_exists(conn, table)
            ]
            if existing_targets:
                print(
                    f"refusing to apply {filename}: it unconditionally drops "
                    f"{', '.join(existing_targets)}, which already exist and are "
                    "not recorded in schema_migrations; assuming they hold data"
                )
                return 1

            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)", (filename,)
                )
            conn.commit()
            print(f"applied {filename}")

        return 0
    finally:
        conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    dry_run = "--dry-run" in argv
    return run(dry_run=dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
