"""Tests for `scripts/migrate_pg.py` and `db/migrations/postgres.manifest`
(feature 10a).

No database required anywhere in this file — every path exercised here is
reachable without opening a connection.
"""

from __future__ import annotations

import pathlib
import re

import scripts.migrate_pg as migrate_pg

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
MANIFEST_PATH = MIGRATIONS_DIR / "postgres.manifest"

_DROP_TABLE_RE = re.compile(r"^DROP TABLE", re.MULTILINE)


def _non_sqlite_migrations() -> set[str]:
    return {p.name for p in MIGRATIONS_DIR.glob("*.sql") if not p.name.endswith("_sqlite.sql")}


def _manifest_entries() -> list[str]:
    entries = []
    for line in MANIFEST_PATH.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.append(stripped)
    return entries


def _derive_destructive_migration_filenames() -> set[str]:
    filenames = set()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name.endswith("_sqlite.sql"):
            continue
        if _DROP_TABLE_RE.search(path.read_text()):
            filenames.add(path.name)
    return filenames


# ---------------------------------------------------------------------------
# Manifest coverage / prefix invariant
# ---------------------------------------------------------------------------


def test_manifest_covers_every_postgres_migration_on_disk() -> None:
    assert set(_manifest_entries()) == _non_sqlite_migrations()


def test_manifest_contains_no_sqlite_entry() -> None:
    entries = _manifest_entries()
    assert entries, "manifest parsed to an empty entry list — the check would be vacuous"
    for entry in entries:
        assert not entry.endswith("_sqlite.sql")


def test_no_duplicate_numeric_prefixes_outside_sqlite_siblings() -> None:
    prefix_re = re.compile(r"^(\d+)_")
    by_prefix: dict[str, list[str]] = {}
    for path in MIGRATIONS_DIR.glob("*.sql"):
        match = prefix_re.match(path.name)
        if not match:
            continue
        by_prefix.setdefault(match.group(1), []).append(path.name)

    assert by_prefix, "no prefixed migrations found — the invariant would be vacuous"

    for prefix, names in by_prefix.items():
        if len(names) == 1:
            continue
        # A shared prefix is only legal for a dialect PAIR: exactly two files,
        # one of which is literally the other's `_sqlite` sibling.
        non_sqlite = [n for n in names if not n.endswith("_sqlite.sql")]
        sqlite_names = [n for n in names if n.endswith("_sqlite.sql")]
        assert len(non_sqlite) == 1 and len(sqlite_names) == 1, (
            f"prefix {prefix} used by non-sibling files: {sorted(names)}"
        )
        expected_sibling = non_sqlite[0][: -len(".sql")] + "_sqlite.sql"
        assert sqlite_names[0] == expected_sibling, (
            f"prefix {prefix} shared by {sorted(names)}, which are not a dialect pair"
        )


# ---------------------------------------------------------------------------
# Docstring hazard content
# ---------------------------------------------------------------------------


def test_docstring_flags_data_loss_hazard() -> None:
    doc = migrate_pg.__doc__ or ""
    assert "DROP TABLE" in doc
    assert "data loss" in doc


def test_docstring_names_every_destructive_migration() -> None:
    doc = migrate_pg.__doc__ or ""
    destructive = _derive_destructive_migration_filenames()
    assert destructive, (
        "no destructive migration derived from db/migrations/ — the docstring "
        "check would be vacuous"
    )
    for filename in destructive:
        assert filename in doc, f"{filename} contains DROP TABLE but is not named in the docstring"


# ---------------------------------------------------------------------------
# Destructive-migration detection and the refuse-to-run guard, exercised
# against the REAL files in `db/migrations/` — never synthetic fixtures.
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Minimal DB-API cursor over an in-memory description of a database."""

    def __init__(self, conn: "_FakeConnection") -> None:
        self._conn = conn
        self._result = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        if "information_schema.tables" in sql:
            self._result = (params[0] in self._conn.existing_tables,)
        elif "FROM schema_migrations" in sql:
            self._result = (1,) if params[0] in self._conn.applied else None
        else:
            self._conn.executed.append(sql)
            self._result = None

    def fetchone(self):
        return self._result


class _FakeConnection:
    def __init__(self, existing_tables: set[str], applied: set[str] | None = None) -> None:
        self.existing_tables = existing_tables
        self.applied = applied or set()
        self.executed: list[str] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


def _first_destructive_manifest_entry() -> str:
    for entry in _manifest_entries():
        if migrate_pg.is_destructive_migration(MIGRATIONS_DIR / entry):
            return entry
    raise AssertionError("no manifested migration is destructive")


def test_destructive_detection_matches_real_migration_files() -> None:
    """The detector must agree with the grep rule over the real directory."""
    by_grep = _derive_destructive_migration_filenames()
    assert by_grep, "no destructive migration on disk — this test would be vacuous"

    by_detector = {
        path.name
        for path in MIGRATIONS_DIR.glob("*.sql")
        if not path.name.endswith("_sqlite.sql")
        and migrate_pg.is_destructive_migration(path)
    }
    assert by_detector == by_grep

    # Each destructive file names at least one table it drops.
    for filename in sorted(by_grep):
        dropped = migrate_pg.dropped_tables((MIGRATIONS_DIR / filename).read_text())
        assert dropped, f"{filename} matches DROP TABLE but no table name was extracted"


def test_guard_refuses_to_run_real_destructive_migration(monkeypatch, capsys) -> None:
    """The refuse-to-run branch fires for a REAL destructive migration.

    The manifest, the migrations directory and the migration text are all the
    shipped ones; only the database is faked, and it reports that the tables
    the migration drops already exist while `schema_migrations` holds no row
    for that file. The runner must refuse before executing any migration SQL.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)

    entry = _first_destructive_manifest_entry()
    dropped = migrate_pg.dropped_tables((MIGRATIONS_DIR / entry).read_text())
    assert dropped

    conn = _FakeConnection(existing_tables=set(dropped), applied=set())
    monkeypatch.setattr(migrate_pg.pg, "connect", lambda: conn)

    exit_code = migrate_pg.run(dry_run=False)
    assert exit_code != 0, "the destructive-migration guard did not fire"

    captured = capsys.readouterr()
    assert "refusing to apply" in captured.out
    assert entry in captured.out

    # Nothing but the schema_migrations bookkeeping table was executed.
    assert all("schema_migrations" in sql for sql in conn.executed), (
        "migration SQL was executed despite the guard"
    )
    assert not any(f"applied {entry}" in line for line in captured.out.splitlines())


def test_guard_does_not_fire_when_dropped_tables_are_absent(monkeypatch, capsys) -> None:
    """Counterpart: with an empty database the same migration is applied."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    entry = _first_destructive_manifest_entry()
    conn = _FakeConnection(existing_tables=set(), applied=set())
    monkeypatch.setattr(migrate_pg.pg, "connect", lambda: conn)

    exit_code = migrate_pg.run(dry_run=False)
    assert exit_code == 0
    assert f"applied {entry}" in capsys.readouterr().out


def test_guard_detection_is_not_tied_to_create_table_wording() -> None:
    """A file that only CREATEs tables is not treated as destructive."""
    non_destructive = {
        path.name
        for path in MIGRATIONS_DIR.glob("*.sql")
        if not migrate_pg.is_destructive_migration(path)
    }
    assert non_destructive, "no non-destructive migration on disk"
    for filename in sorted(non_destructive):
        assert not _DROP_TABLE_RE.search((MIGRATIONS_DIR / filename).read_text())


# ---------------------------------------------------------------------------
# Manifest parser
# ---------------------------------------------------------------------------


def test_parse_manifest_ignores_blanks_and_comments_and_preserves_order(
    tmp_path: pathlib.Path,
) -> None:
    manifest = tmp_path / "postgres.manifest"
    manifest.write_text(
        "\n".join(
            [
                "# a leading comment",
                "",
                "001_first.sql",
                "  ",
                "# another comment",
                "002_second.sql",
                "003_third.sql",
                "",
            ]
        )
    )
    assert migrate_pg.parse_manifest(manifest) == [
        "001_first.sql",
        "002_second.sql",
        "003_third.sql",
    ]


def test_run_exits_nonzero_naming_missing_file_before_opening_connection(
    tmp_path: pathlib.Path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    fake_migrations_dir = tmp_path
    (fake_migrations_dir / "001_present.sql").write_text("SELECT 1;")
    manifest = fake_migrations_dir / "postgres.manifest"
    manifest.write_text("001_present.sql\n002_missing.sql\n")

    monkeypatch.setattr(migrate_pg, "MIGRATIONS_DIR", fake_migrations_dir)
    monkeypatch.setattr(migrate_pg, "MANIFEST_PATH", manifest)

    def _fail_if_called() -> None:
        raise AssertionError("connect() must not be called when a manifest file is missing")

    monkeypatch.setattr(migrate_pg.pg, "connect", _fail_if_called)

    exit_code = migrate_pg.run(dry_run=False)
    assert exit_code != 0

    captured = capsys.readouterr()
    assert "002_missing.sql" in captured.out


def test_dry_run_prints_plan_and_opens_no_connection(
    tmp_path: pathlib.Path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    fake_migrations_dir = tmp_path
    (fake_migrations_dir / "001_present.sql").write_text("SELECT 1;")
    manifest = fake_migrations_dir / "postgres.manifest"
    manifest.write_text("001_present.sql\n")

    monkeypatch.setattr(migrate_pg, "MIGRATIONS_DIR", fake_migrations_dir)
    monkeypatch.setattr(migrate_pg, "MANIFEST_PATH", manifest)

    def _fail_if_called() -> None:
        raise AssertionError("connect() must not be called in --dry-run")

    monkeypatch.setattr(migrate_pg.pg, "connect", _fail_if_called)

    exit_code = migrate_pg.run(dry_run=True)
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "001_present.sql" in captured.out
