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
    for entry in _manifest_entries():
        assert not entry.endswith("_sqlite.sql")


def test_no_duplicate_numeric_prefixes_outside_sqlite_siblings() -> None:
    prefix_re = re.compile(r"^(\d+)_")
    by_prefix: dict[str, list[str]] = {}
    for path in MIGRATIONS_DIR.glob("*.sql"):
        match = prefix_re.match(path.name)
        if not match:
            continue
        by_prefix.setdefault(match.group(1), []).append(path.name)

    for prefix, names in by_prefix.items():
        if len(names) == 1:
            continue
        # Every extra name sharing this prefix must be the `_sqlite` sibling
        # of one of the others.
        non_sqlite = [n for n in names if not n.endswith("_sqlite.sql")]
        assert len(non_sqlite) <= 1, f"prefix {prefix} used by non-sibling files: {names}"


# ---------------------------------------------------------------------------
# Docstring hazard content
# ---------------------------------------------------------------------------


def test_docstring_flags_data_loss_hazard() -> None:
    doc = migrate_pg.__doc__ or ""
    assert "DROP TABLE" in doc
    assert "data loss" in doc


def test_docstring_names_every_destructive_migration() -> None:
    doc = migrate_pg.__doc__ or ""
    for filename in _derive_destructive_migration_filenames():
        assert filename in doc, f"{filename} contains DROP TABLE but is not named in the docstring"


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
