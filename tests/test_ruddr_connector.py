"""Tests for the RUDDR connector."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from connectors.base import (
    AuthToken,
    ConnectorInterface,
    DateRange,
    WriteProposal,
)
from connectors.ruddr import (
    ConnectorError,
    InMemoryTokenStore,
    RateLimiter,
    RUDDRConnector,
)


FIXTURE_PATH = str(
    Path(__file__).parent / "fixtures" / "ruddr_entities.json"
)


class FakeHTTP:
    def __init__(
        self,
        get_responses: Optional[List[Dict[str, Any]]] = None,
        raise_on_get: Optional[Exception] = None,
    ) -> None:
        self.get_calls: List[tuple[str, Dict[str, str]]] = []
        self.post_calls: List[tuple[str, Dict[str, str], Dict[str, Any]]] = []
        self._get_responses = list(get_responses or [])
        self._raise_on_get = raise_on_get

    def get(self, url: str, headers: Dict[str, str]) -> Dict[str, Any]:
        self.get_calls.append((url, headers))
        if self._raise_on_get:
            raise self._raise_on_get
        return self._get_responses.pop(0) if self._get_responses else {}

    def post(self, url: str, headers: Dict[str, str], data: Dict[str, Any]) -> Dict[str, Any]:
        self.post_calls.append((url, headers, data))
        return {}


def _make_connector(**overrides: Any) -> RUDDRConnector:
    defaults = {
        "tenant_id": "tenant-test",
        "api_key": "ruddr-key",
        "fixture_path": FIXTURE_PATH,
    }
    defaults.update(overrides)
    return RUDDRConnector(**defaults)


# ---------------------------------------------------------------------------
# Interface compliance
# ---------------------------------------------------------------------------


def test_implements_connector_interface() -> None:
    c = _make_connector()
    assert isinstance(c, ConnectorInterface)


def test_category_is_psa() -> None:
    assert RUDDRConnector.category == "psa"


def test_module_importable() -> None:
    from connectors.ruddr import RUDDRConnector  # noqa: F401


# ---------------------------------------------------------------------------
# Auth — API key path
# ---------------------------------------------------------------------------


def test_authenticate_returns_api_key_token() -> None:
    c = _make_connector()
    tok = c.authenticate()
    assert tok.provider == "ruddr"
    assert tok.category == "psa"
    assert tok.access_token == "ruddr-key"
    # Second call returns cached value, not regenerated.
    tok2 = c.authenticate()
    assert tok2 is tok or tok2.access_token == tok.access_token


def test_authenticate_without_key_raises() -> None:
    c = RUDDRConnector(tenant_id="tenant-test", api_key="")
    with pytest.raises(ConnectorError):
        c.authenticate()


# ---------------------------------------------------------------------------
# read_entities (fixture-mode)
# ---------------------------------------------------------------------------


def test_read_entities_client_returns_normalized() -> None:
    c = _make_connector()
    entities = c.read_entities("client", {})
    assert len(entities) == 17
    sample = entities[0]
    assert sample.source == "ruddr"
    assert sample.category == "psa"
    assert sample.entity_category == "organization"
    assert sample.raw_record["type"] == "client"
    # slug preserved in raw_record for matcher signal extraction.
    assert "slug" in sample.raw_record


def test_read_entities_vendor_returns_normalized() -> None:
    c = _make_connector()
    entities = c.read_entities("vendor", {})
    assert len(entities) == 3
    for e in entities:
        assert e.entity_category == "organization"
        assert e.raw_record["type"] == "vendor"


def test_read_entities_person_returns_team_members() -> None:
    c = _make_connector()
    entities = c.read_entities("person", {})
    assert len(entities) == 25
    for e in entities:
        assert e.entity_category == "person"
        assert e.raw_record["type"] == "team-member"
        assert e.email is not None  # team members carry email + department
        assert e.email_is_person is True


def test_all_45_fixture_entities_load_without_error() -> None:
    c = _make_connector()
    loaded = (
        c.read_entities("client", {})
        + c.read_entities("vendor", {})
        + c.read_entities("person", {})
    )
    assert len(loaded) == 45
    for e in loaded:
        assert e.normalized_name
        assert e.source_id


def test_read_entities_unsupported_type_raises() -> None:
    c = _make_connector()
    with pytest.raises(ConnectorError):
        c.read_entities("project", {})


def test_filter_department() -> None:
    c = _make_connector()
    entities = c.read_entities("person", {"department": "Data Engineering"})
    assert len(entities) >= 1
    assert all(
        e.raw_record["department"] == "Data Engineering" for e in entities
    )


def test_filter_email_contains() -> None:
    c = _make_connector()
    entities = c.read_entities("person", {"email_contains": "@clientcorp.com"})
    assert all(
        e.email and "@clientcorp.com" in e.email for e in entities
    )


# ---------------------------------------------------------------------------
# Operational records — project flattening + project→client edge
# ---------------------------------------------------------------------------


def test_read_operational_records_returns_projects() -> None:
    c = _make_connector()
    projects = c.read_operational_records("project", {})
    assert len(projects) == 42  # 36 client-owned + 6 vendor-owned in fixture
    for p in projects:
        assert p.record_type == "project"
        assert p.source == "ruddr"
        assert p.category == "psa"
        assert p.attributes.get("code")
        # parent_source_id is the cross-category edge candidate.
        assert p.parent_source_id
        assert p.parent_source_id == p.attributes["client_source_id"]


def test_project_code_preserved_for_matcher() -> None:
    """Project code structure (e.g. 'CEN-GP-SOW1') is preserved verbatim
    so matcher Stage 3 can parse the client-prefix signal."""
    c = _make_connector()
    projects = c.read_operational_records("project", {})
    codes = [p.attributes["code"] for p in projects]
    assert "CEN-GP-SOW1" in codes
    # And the project pointing at it has the Cenlar parent.
    cen = next(p for p in projects if p.attributes["code"] == "CEN-GP-SOW1")
    assert cen.parent_source_id == "RUDDR-001"
    assert "CEN" in cen.attributes["code"].split("-")[0]


def test_project_to_client_relationship_extractable() -> None:
    """Brief SC: project-to-client relationships extractable from output."""
    c = _make_connector()
    clients = {e.source_id: e for e in c.read_entities("client", {})}
    projects = c.read_operational_records("project", {})
    # Every project owned by a client (parent_source_id starts RUDDR-) has a
    # matching client entity.
    client_projects = [p for p in projects if p.parent_source_id in clients]
    assert len(client_projects) == 36


def test_project_filter_status() -> None:
    c = _make_connector()
    active = c.read_operational_records("project", {"status": "active"})
    assert all(p.attributes["status"] == "active" for p in active)
    assert 0 < len(active) < 42


def test_unsupported_operational_record_type_raises() -> None:
    c = _make_connector()
    with pytest.raises(ConnectorError):
        c.read_operational_records("class", {})


# ---------------------------------------------------------------------------
# Shadow Ledger write semantics
# ---------------------------------------------------------------------------


def test_execute_write_returns_shadow_preview() -> None:
    c = _make_connector()
    proposal = WriteProposal(
        proposal_id="p-1",
        tenant_id="tenant-test",
        target_source="ruddr",
        target_category="psa",
        operation="create",
        target_source_id=None,
        payload={"display_name": "Acme"},
    )
    result = c.execute_write(proposal)
    assert result.executed is False
    assert result.shadow is True
    assert result.preview["operation"] == "create"
    assert "Shadow Ledger preview" in result.preview["note"]


def test_execute_write_never_calls_http() -> None:
    http = FakeHTTP()
    c = RUDDRConnector(
        tenant_id="tenant-test",
        api_key="k",
        http_client=http,
        rate_limiter=RateLimiter(120, 60.0, clock=lambda: 0.0, sleep_fn=lambda s: None),
    )
    proposal = WriteProposal(
        proposal_id="p-2", tenant_id="tenant-test",
        target_source="ruddr", target_category="psa",
        operation="update", target_source_id="RUDDR-001",
        payload={"display_name": "Renamed"},
    )
    c.execute_write(proposal)
    assert http.get_calls == []
    assert http.post_calls == []


def test_validate_write_rejects_wrong_target() -> None:
    c = _make_connector()
    bad = WriteProposal(
        proposal_id="p-3", tenant_id="tenant-test",
        target_source="quickbooks", target_category="accounting",
        operation="create", target_source_id=None, payload={},
    )
    result = c.validate_write(bad)
    assert result.is_valid is False


def test_rollback_shadow_preview() -> None:
    c = _make_connector()
    proposal = WriteProposal(
        proposal_id="p-4", tenant_id="tenant-test",
        target_source="ruddr", target_category="psa",
        operation="create", target_source_id=None, payload={},
    )
    wr = c.execute_write(proposal)
    rb = c.rollback_write(wr)
    assert rb.rolled_back is True


# ---------------------------------------------------------------------------
# CSV fallback
# ---------------------------------------------------------------------------


def test_export_csv_fallback_produces_valid_csv() -> None:
    c = _make_connector()
    export = c.export_csv_fallback("client", DateRange("2024-01-01", "2025-12-31"))
    assert export.row_count == 17
    reader = csv.DictReader(io.StringIO(export.csv_text))
    rows = list(reader)
    assert len(rows) == 17
    assert all(r["source"] == "ruddr" for r in rows)


# ---------------------------------------------------------------------------
# Live-API path mapping + error redaction
# ---------------------------------------------------------------------------


def test_read_entities_live_api_shape() -> None:
    api_resp = {
        "results": [
            {
                "id": "R-1",
                "source": "ruddr",
                "entity_category": "organization",
                "slug": "cenlar-fsb",
                "display_name": "Cenlar FSB",
                "type": "client",
            }
        ]
    }
    http = FakeHTTP(get_responses=[api_resp])
    c = RUDDRConnector(
        tenant_id="tenant-test",
        api_key="k",
        http_client=http,
        rate_limiter=RateLimiter(120, 60.0, clock=lambda: 0.0, sleep_fn=lambda s: None),
    )
    out = c.read_entities("client", {})
    assert len(out) == 1
    assert out[0].source_id == "R-1"
    assert out[0].normalized_name == "cenlar fsb"


def test_read_failure_does_not_leak_raw_error() -> None:
    http = FakeHTTP(raise_on_get=RuntimeError("RUDDR 500: api_key=secret-xyz"))
    c = RUDDRConnector(
        tenant_id="tenant-test",
        api_key="k",
        http_client=http,
        rate_limiter=RateLimiter(120, 60.0, clock=lambda: 0.0, sleep_fn=lambda s: None),
    )
    with pytest.raises(ConnectorError) as ei:
        c.read_entities("client", {})
    assert "secret-xyz" not in str(ei.value)
