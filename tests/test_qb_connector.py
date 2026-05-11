"""Tests for the QuickBooks Online connector.

Exercises the fixture-mode path (default for V1 ship), the OAuth refresh
path (via injected mock HTTP client), the rate limiter (via injected
clock / sleep), Shadow Ledger write semantics, and the CSV export.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from connectors.base import (
    AuthToken,
    ConnectorInterface,
    DateRange,
    WriteProposal,
)
from connectors.quickbooks import (
    ConnectorError,
    InMemoryTokenStore,
    QuickBooksConnector,
    RateLimiter,
)


FIXTURE_PATH = str(
    Path(__file__).parent / "fixtures" / "qb_entities.json"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeHTTP:
    def __init__(
        self,
        get_responses: Optional[List[Dict[str, Any]]] = None,
        post_response: Optional[Dict[str, Any]] = None,
        raise_on_get: Optional[Exception] = None,
    ) -> None:
        self.get_calls: List[tuple[str, Dict[str, str]]] = []
        self.post_calls: List[tuple[str, Dict[str, str], Dict[str, Any]]] = []
        self._get_responses = list(get_responses or [])
        self._post_response = post_response or {
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "expires_at": "2099-01-01T00:00:00Z",
        }
        self._raise_on_get = raise_on_get

    def get(self, url: str, headers: Dict[str, str]) -> Dict[str, Any]:
        self.get_calls.append((url, headers))
        if self._raise_on_get:
            raise self._raise_on_get
        return self._get_responses.pop(0) if self._get_responses else {}

    def post(
        self, url: str, headers: Dict[str, str], data: Dict[str, Any]
    ) -> Dict[str, Any]:
        self.post_calls.append((url, headers, data))
        return self._post_response


def _make_connector(**overrides: Any) -> QuickBooksConnector:
    defaults = {
        "tenant_id": "tenant-test",
        "client_id": "cid",
        "client_secret": "csecret",
        "realm_id": "9999",
        "fixture_path": FIXTURE_PATH,
    }
    defaults.update(overrides)
    return QuickBooksConnector(**defaults)


# ---------------------------------------------------------------------------
# Interface compliance
# ---------------------------------------------------------------------------


def test_implements_connector_interface() -> None:
    c = _make_connector()
    assert isinstance(c, ConnectorInterface)


def test_category_is_accounting() -> None:
    assert QuickBooksConnector.category == "accounting"


def test_module_importable() -> None:
    """Brief SC: `python -c "from connectors.quickbooks import QuickBooksConnector"`
    exits 0."""
    from connectors.quickbooks import QuickBooksConnector  # noqa: F401


# ---------------------------------------------------------------------------
# read_entities (fixture-mode)
# ---------------------------------------------------------------------------


def test_read_entities_customer_returns_normalized() -> None:
    c = _make_connector()
    entities = c.read_entities("customer", {})
    assert len(entities) == 16
    sample = entities[0]
    assert sample.source == "quickbooks"
    assert sample.category == "accounting"
    assert sample.entity_category == "organization"
    assert sample.normalized_name  # non-empty
    assert sample.raw_record["type"] == "Customer"


def test_read_entities_vendor_returns_normalized() -> None:
    c = _make_connector()
    entities = c.read_entities("vendor", {})
    assert len(entities) == 5
    for e in entities:
        assert e.entity_category == "organization"
        assert e.raw_record["type"] == "Vendor"


def test_read_entities_person_returns_employees() -> None:
    c = _make_connector()
    entities = c.read_entities("person", {})
    assert len(entities) == 25
    for e in entities:
        assert e.entity_category == "person"
        assert e.raw_record["type"] == "Employee"
        assert e.email_is_person is True


def test_read_entities_unsupported_type_raises() -> None:
    c = _make_connector()
    with pytest.raises(ConnectorError):
        c.read_entities("contract", {})


def test_all_46_fixture_entities_load_without_error() -> None:
    """Brief SC: all 46 QB fixture entities load through the connector
    mapping without errors."""
    c = _make_connector()
    loaded = (
        c.read_entities("customer", {})
        + c.read_entities("vendor", {})
        + c.read_entities("person", {})
    )
    assert len(loaded) == 46
    # Every record has a non-empty normalized_name and source_id.
    for e in loaded:
        assert e.normalized_name
        assert e.source_id


def test_filter_display_name_contains() -> None:
    c = _make_connector()
    entities = c.read_entities("customer", {"display_name_contains": "cenlar"})
    assert len(entities) >= 1
    assert all("cenlar" in e.raw_name.lower() for e in entities)


def test_filter_email_contains() -> None:
    c = _make_connector()
    entities = c.read_entities("customer", {"email_contains": "@cenlarfsb.com"})
    assert any(e.email and "@cenlarfsb.com" in e.email for e in entities)


# ---------------------------------------------------------------------------
# Live-API path mapping (PascalCase QB JSON shape)
# ---------------------------------------------------------------------------


def test_read_entities_maps_quickbooks_api_shape() -> None:
    """Defensive mapping: connector accepts the real QB API JSON
    (PascalCase, nested email), not just our fixture shape."""
    api_resp = {
        "QueryResponse": {
            "Customer": [
                {
                    "Id": "1",
                    "DisplayName": "Cenlar, LLC.",
                    "PrimaryEmailAddr": {"Address": "billing@cenlarfsb.com"},
                }
            ]
        }
    }
    http = FakeHTTP(get_responses=[api_resp])
    store = InMemoryTokenStore()
    store.put(
        AuthToken(
            provider="quickbooks",
            category="accounting",
            tenant_id="tenant-test",
            access_token="tok",
        )
    )
    c = QuickBooksConnector(
        tenant_id="tenant-test",
        realm_id="9999",
        http_client=http,
        token_store=store,
        rate_limiter=RateLimiter(500, 60.0, clock=lambda: 0.0, sleep_fn=lambda s: None),
    )
    out = c.read_entities("customer", {})
    assert len(out) == 1
    assert out[0].source_id == "1"
    assert out[0].normalized_name == "cenlar"
    assert out[0].email == "billing@cenlarfsb.com"


# ---------------------------------------------------------------------------
# OAuth refresh
# ---------------------------------------------------------------------------


def test_refresh_token_on_expired() -> None:
    """An expired token triggers a refresh via the injected HTTP client and
    the new token is persisted."""
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    store = InMemoryTokenStore()
    store.put(
        AuthToken(
            provider="quickbooks",
            category="accounting",
            tenant_id="tenant-test",
            access_token="expired",
            refresh_token="r1",
            expires_at=past,
        )
    )
    http = FakeHTTP(
        post_response={
            "access_token": "fresh-token",
            "refresh_token": "r2",
            "expires_at": "2099-01-01T00:00:00Z",
        }
    )
    c = QuickBooksConnector(
        tenant_id="tenant-test",
        realm_id="9999",
        http_client=http,
        token_store=store,
    )
    token = c.authenticate()
    assert token.access_token == "fresh-token"
    assert token.refresh_token == "r2"
    assert len(http.post_calls) == 1


def test_authenticate_without_token_raises() -> None:
    c = _make_connector()
    with pytest.raises(ConnectorError):
        c.authenticate()


def test_refresh_failure_redacted() -> None:
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    store = InMemoryTokenStore()
    store.put(
        AuthToken(
            provider="quickbooks",
            category="accounting",
            tenant_id="tenant-test",
            access_token="expired",
            refresh_token="r1",
            expires_at=past,
        )
    )

    class FailingHTTP:
        def get(self, url: str, headers: Dict[str, str]) -> Dict[str, Any]:
            return {}

        def post(
            self, url: str, headers: Dict[str, str], data: Dict[str, Any]
        ) -> Dict[str, Any]:
            raise RuntimeError("QB internal error: secret=hunter2")

    c = QuickBooksConnector(
        tenant_id="tenant-test",
        realm_id="9999",
        http_client=FailingHTTP(),
        token_store=store,
    )
    with pytest.raises(ConnectorError) as ei:
        c.authenticate()
    # Raw QB body must not leak to the public-facing error message.
    assert "hunter2" not in str(ei.value)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


def test_rate_limiter_caps_requests() -> None:
    """Burst beyond max within the window sleeps until cutoff."""
    sleeps: List[float] = []
    now = [0.0]

    def clock() -> float:
        return now[0]

    def sleep_fn(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    rl = RateLimiter(max_requests=3, window_s=10.0, clock=clock, sleep_fn=sleep_fn)
    for _ in range(3):
        rl.acquire()
    # The 4th call should sleep ~10s.
    rl.acquire()
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(10.0, rel=1e-6)


def test_rate_limiter_exponential_backoff() -> None:
    sleeps: List[float] = []
    rl = RateLimiter(
        max_requests=500,
        window_s=60.0,
        clock=lambda: 0.0,
        sleep_fn=lambda s: sleeps.append(s),
    )
    rl.backoff(1)
    rl.backoff(2)
    rl.backoff(3)
    # 0.5, 1.0, 2.0 — base * 2^(attempt-1)
    assert sleeps == [0.5, 1.0, 2.0]


# ---------------------------------------------------------------------------
# Shadow Ledger write semantics
# ---------------------------------------------------------------------------


def test_execute_write_returns_shadow_preview() -> None:
    c = _make_connector()
    proposal = WriteProposal(
        proposal_id="p-1",
        tenant_id="tenant-test",
        target_source="quickbooks",
        target_category="accounting",
        operation="create",
        target_source_id=None,
        payload={"DisplayName": "Acme Inc"},
    )
    result = c.execute_write(proposal)
    assert result.executed is False
    assert result.shadow is True
    assert result.preview["operation"] == "create"
    assert "Shadow Ledger preview" in result.preview["note"]


def test_execute_write_never_calls_http_client() -> None:
    """The most important V1 invariant: even with a working HTTP client,
    execute_write must not issue a single network call."""
    http = FakeHTTP()
    store = InMemoryTokenStore()
    store.put(
        AuthToken(
            provider="quickbooks", category="accounting",
            tenant_id="tenant-test", access_token="t",
        )
    )
    c = QuickBooksConnector(
        tenant_id="tenant-test",
        realm_id="9999",
        http_client=http,
        token_store=store,
    )
    proposal = WriteProposal(
        proposal_id="p-2", tenant_id="tenant-test",
        target_source="quickbooks", target_category="accounting",
        operation="update", target_source_id="QB-001",
        payload={"DisplayName": "Renamed"},
    )
    c.execute_write(proposal)
    assert http.get_calls == []
    assert http.post_calls == []


def test_validate_write_rejects_wrong_target() -> None:
    c = _make_connector()
    bad = WriteProposal(
        proposal_id="p-3", tenant_id="tenant-test",
        target_source="ruddr", target_category="psa",
        operation="create", target_source_id=None, payload={},
    )
    result = c.validate_write(bad)
    assert result.is_valid is False
    assert any("target_source" in i for i in result.issues)


def test_rollback_shadow_preview() -> None:
    c = _make_connector()
    proposal = WriteProposal(
        proposal_id="p-4", tenant_id="tenant-test",
        target_source="quickbooks", target_category="accounting",
        operation="create", target_source_id=None, payload={},
    )
    wr = c.execute_write(proposal)
    rb = c.rollback_write(wr)
    assert rb.rolled_back is True


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


def test_export_csv_fallback_produces_valid_csv() -> None:
    c = _make_connector()
    export = c.export_csv_fallback("customer", DateRange("2024-01-01", "2025-12-31"))
    assert export.row_count == 16
    assert export.entity_type == "customer"
    # Parse the CSV back and confirm headers match dataclass-derived fields.
    reader = csv.DictReader(io.StringIO(export.csv_text))
    rows = list(reader)
    assert len(rows) == 16
    assert "normalized_name" in (reader.fieldnames or [])
    assert all(r["source"] == "quickbooks" for r in rows)


# ---------------------------------------------------------------------------
# Error redaction
# ---------------------------------------------------------------------------


def test_read_failure_does_not_leak_raw_qb_error() -> None:
    store = InMemoryTokenStore()
    store.put(
        AuthToken(
            provider="quickbooks", category="accounting",
            tenant_id="tenant-test", access_token="t",
        )
    )
    http = FakeHTTP(raise_on_get=RuntimeError("QB 500: secret-token=abc123"))
    c = QuickBooksConnector(
        tenant_id="tenant-test",
        realm_id="9999",
        http_client=http,
        token_store=store,
        rate_limiter=RateLimiter(500, 60.0, clock=lambda: 0.0, sleep_fn=lambda s: None),
    )
    with pytest.raises(ConnectorError) as ei:
        c.read_entities("customer", {})
    assert "secret-token" not in str(ei.value)
    assert "abc123" not in str(ei.value)
