"""Tests for the Dash application shell (feature 16).

No database access anywhere in this module — the shell, sidebar, and page
structure contracts are all pure/import-time properties.
"""

from __future__ import annotations

import inspect
import pathlib
import re
import sys

import dash
import pytest
from dash import dash_table

import dashboard.app as dashboard_app

# Dash's page loader imports every module under `dashboard/pages/` as
# `pages.<name>` (relative to `pages_folder="pages"`), registering it with
# `dash.register_page`. Importing `dashboard.pages.connectors` directly as
# well would re-trigger `dash.register_page` under a second module name and
# collide on path — so these modules are pulled from the already-loaded
# `sys.modules` entry instead of a fresh top-level import.
connectors_page = sys.modules["pages.connectors"]
audit_log_page = sys.modules["pages.audit_log"]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGES_DIR = REPO_ROOT / "dashboard" / "pages"


def test_app_is_dash_with_pages_enabled():
    assert isinstance(dashboard_app.app, dash.Dash)
    assert dashboard_app.app.use_pages is True


def test_every_registered_path_returns_200():
    client = dashboard_app.app.server.test_client()
    for entry in dash.page_registry.values():
        resp = client.get(entry["path"])
        assert resp.status_code == 200, entry["path"]


def test_this_features_paths_are_registered():
    paths = {entry["path"] for entry in dash.page_registry.values()}
    assert {"/connectors", "/audit-log"} <= paths


def test_no_callbacks_registered_in_shell():
    src = (REPO_ROOT / "dashboard" / "app.py").read_text()
    assert not re.search(r"@app\.callback|@callback|clientside_callback", src)


def test_build_sidebar_is_pure():
    fixture_registry = {
        "pages.a": {"path": "/a", "name": "A"},
        "pages.b": {"path": "/b", "name": "B"},
    }
    tree1 = dashboard_app.build_sidebar(fixture_registry)
    tree2 = dashboard_app.build_sidebar(fixture_registry)
    assert repr(tree1) == repr(tree2)


def _collect_ids(component):
    ids = []
    comp_id = getattr(component, "id", None)
    if comp_id is not None:
        ids.append(comp_id)
    children = getattr(component, "children", None)
    if isinstance(children, list):
        for child in children:
            ids.extend(_collect_ids(child))
    elif children is not None and hasattr(children, "id"):
        ids.extend(_collect_ids(children))
    return ids


def test_sidebar_contract_against_real_registry():
    tree = dashboard_app.build_sidebar(dash.page_registry)
    all_ids = _collect_ids(tree)

    assert "app-sidebar" in all_ids

    for entry in dash.page_registry.values():
        path = entry["path"]
        nav_link_id = {"type": "nav-link", "path": path}
        nav_badge_id = {"type": "nav-badge", "path": path}
        assert all_ids.count(nav_link_id) == 1, path
        assert all_ids.count(nav_badge_id) == 1, path

    assert {"type": "nav-badge", "path": "/approval-queue"} in all_ids


def test_existing_pages_keep_their_paths():
    import dashboard.app  # noqa: F401  (ensure pages are registered)

    registered = {
        entry["module"].rsplit(".", 1)[-1]: entry["path"]
        for entry in dash.page_registry.values()
    }

    expected = {}
    pattern = re.compile(r"dash\.register_page\(\s*__name__\s*,\s*path\s*=\s*[\"']([^\"']+)[\"']")
    for path in PAGES_DIR.glob("*.py"):
        if path.name == "__init__.py":
            continue
        m = pattern.search(path.read_text())
        assert m, f"no dash.register_page(path=...) found in {path}"
        expected[path.stem] = m.group(1)

    assert registered == expected
    assert expected["connectors"] == "/connectors"
    assert expected["audit_log"] == "/audit-log"

    import subprocess

    for name, path in expected.items():
        result = subprocess.run(
            ["git", "show", f"HEAD:dashboard/pages/{name}.py"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            continue  # module not present in HEAD — exempt
        m = pattern.search(result.stdout)
        assert m and m.group(1) == path, name


# --- connectors.py -----------------------------------------------------

FIXTURE_CONNECTORS = [
    {
        "provider": "quickbooks",
        "category": "accounting",
        "connected": True,
        "last_sync": None,
        "last_sync_status": None,
        "last_sync_error": None,
    },
    {
        "provider": "ruddr",
        "category": "psa",
        "connected": True,
        "last_sync": None,
        "last_sync_status": None,
        "last_sync_error": None,
    },
    {
        "provider": "another_accounting_tool",
        "category": "accounting",
        "connected": False,
        "last_sync": None,
        "last_sync_status": None,
        "last_sync_error": None,
    },
]


def test_build_category_groups():
    groups = connectors_page.build_category_groups(FIXTURE_CONNECTORS)
    assert len(groups) == 2  # accounting, psa

    categories = {g.id["category"] for g in groups}
    assert categories == {"accounting", "psa"}

    seen_providers = []
    for group in groups:
        for provider_id in _collect_ids(group):
            if isinstance(provider_id, dict) and provider_id.get("type") == "connector-card":
                seen_providers.append(provider_id["provider"])
    assert sorted(seen_providers) == sorted(c["provider"] for c in FIXTURE_CONNECTORS)
    assert len(seen_providers) == len(set(seen_providers))


def test_connectors_layout_is_zero_arg_callable():
    tree = connectors_page.layout()
    assert tree is not None


# --- audit_log.py --------------------------------------------------------

PG_SCHEMA = REPO_ROOT / "db" / "schema.sql"


def _audit_log_ddl_columns() -> set:
    sql = PG_SCHEMA.read_text()
    sql = re.sub(r"--[^\n]*", "", sql)
    m = re.search(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?audit_log\s*\((.*?)\)\s*;",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert m, "CREATE TABLE audit_log not found in db/schema.sql"
    cols = set()
    for line in m.group(1).split(","):
        stripped = line.strip()
        if not stripped:
            continue
        first = stripped.split()[0]
        cols.add(first)
    return cols


def test_audit_log_layout_table_contract():
    tree = audit_log_page.layout()
    ids = _collect_ids(tree)

    tables = [c for c in _flatten(tree) if isinstance(c, dash_table.DataTable)]
    assert len(tables) == 1
    table = tables[0]
    assert table.id == "audit-log-table"

    displayed = [col["id"] for col in table.columns]
    assert displayed == [
        "created_at",
        "actor_id",
        "action",
        "resource",
        "resource_id",
        "category",
        "diff",
    ]
    assert set(displayed) <= _audit_log_ddl_columns()

    for filter_id in (
        "audit-filter-date-range",
        "audit-filter-action",
        "audit-filter-resource",
        "audit-filter-category",
        "audit-filter-actor",
    ):
        assert filter_id in ids


def _flatten(component):
    out = [component]
    children = getattr(component, "children", None)
    if isinstance(children, list):
        for child in children:
            out.extend(_flatten(child))
    elif children is not None and hasattr(children, "children"):
        out.extend(_flatten(children))
    return out


AUDIT_ROWS = [
    {
        "created_at": "2026-01-01",
        "actor_id": None,
        "action": "connector.sync",
        "resource": "connectors",
        "resource_id": "quickbooks",
        "category": "accounting",
        "diff": None,
    },
    {
        "created_at": "2026-01-02",
        "actor_id": None,
        "action": "connector.list",
        "resource": "connectors",
        "resource_id": None,
        "category": None,
        "diff": None,
    },
    {
        "created_at": "2026-01-03",
        "actor_id": "user-42",
        "action": "connector.status",
        "resource": "connectors",
        "resource_id": "ruddr",
        "category": "psa",
        "diff": None,
    },
]


@pytest.mark.parametrize(
    "kwargs,expected_len",
    [
        ({"action": "connector.sync"}, 1),
        ({"resource": "connectors"}, 3),
        ({"category": "accounting"}, 1),
        ({"actor": "user-42"}, 1),
    ],
)
def test_apply_filters(kwargs, expected_len):
    result = audit_log_page.apply_filters(AUDIT_ROWS, **kwargs)
    assert len(result) == expected_len
