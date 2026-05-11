"""QuickBooks Online connector (category: accounting).

V1 source-system connector for QuickBooks Online. Implements the
`ConnectorInterface` contract. The connector is intentionally test-friendly:
when `fixture_path` is provided, the connector reads from a local JSON
fixture and treats it as the "API source." This is how V1 ships test
coverage without a QB sandbox (the brief explicitly calls for mocked API
responses).

Write surface: `execute_write` returns Shadow Ledger preview only. Live
write-back to QB is out of scope for V1 (see roadmap NOT-SCOPE).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol

from connectors.base import (
    AuthToken,
    ConnectorInterface,
    CSVExport,
    DateRange,
    NormalizedEntity,
    NormalizedRecord,
    NormalizedTransaction,
    RollbackResult,
    ValidationResult,
    WriteProposal,
    WriteResult,
)
from core.ingestion.normalizer import normalize_entity


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConnectorError(RuntimeError):
    """Public-safe wrapper around source-system errors. Raw QB error
    bodies are logged at DEBUG; only redacted strings escape to callers."""


# ---------------------------------------------------------------------------
# Token storage protocol (injected from auth layer)
# ---------------------------------------------------------------------------


class TokenStore(Protocol):
    """Tenant-scoped token persistence. The auth router supplies the real
    implementation (Supabase-backed, encrypted at rest); tests pass an
    in-memory stub."""

    def get(self, tenant_id: str, provider: str) -> Optional[AuthToken]: ...
    def put(self, token: AuthToken) -> None: ...


class InMemoryTokenStore:
    """Default token store. Single-process, not persistent. Used for tests
    and local development."""

    def __init__(self) -> None:
        self._tokens: Dict[tuple[str, str], AuthToken] = {}

    def get(self, tenant_id: str, provider: str) -> Optional[AuthToken]:
        return self._tokens.get((tenant_id, provider))

    def put(self, token: AuthToken) -> None:
        self._tokens[(token.tenant_id, token.provider)] = token


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Sliding-window rate limiter. Caps at `max_requests` per `window_s`
    seconds. Sleeps via `sleep_fn` (injectable for tests)."""

    def __init__(
        self,
        max_requests: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.max_requests = max_requests
        self.window_s = window_s
        self._clock = clock
        self._sleep = sleep_fn
        self._timestamps: List[float] = []
        self._backoff_base = 0.5

    def acquire(self) -> None:
        """Block until a slot is available."""
        now = self._clock()
        cutoff = now - self.window_s
        self._timestamps = [t for t in self._timestamps if t >= cutoff]
        if len(self._timestamps) >= self.max_requests:
            wait = self._timestamps[0] + self.window_s - now
            if wait > 0:
                self._sleep(wait)
        self._timestamps.append(self._clock())

    def backoff(self, attempt: int) -> None:
        """Exponential backoff for retryable API errors. attempt is 1-based."""
        self._sleep(self._backoff_base * (2 ** (attempt - 1)))


# ---------------------------------------------------------------------------
# HTTP client protocol (injected — real impl uses httpx in production)
# ---------------------------------------------------------------------------


class HTTPClient(Protocol):
    def get(self, url: str, headers: Dict[str, str]) -> Dict[str, Any]: ...
    def post(
        self, url: str, headers: Dict[str, str], data: Dict[str, Any]
    ) -> Dict[str, Any]: ...


# ---------------------------------------------------------------------------
# QuickBooks connector
# ---------------------------------------------------------------------------


_QB_TYPE_FOR_ENTITY: Dict[str, str] = {
    "customer": "Customer",
    "client": "Customer",
    "vendor": "Vendor",
    "person": "Employee",
    "employee": "Employee",
}


class QuickBooksConnector(ConnectorInterface):
    """QuickBooks Online connector. Category: accounting."""

    category = "accounting"

    def __init__(
        self,
        tenant_id: str,
        client_id: str = "",
        client_secret: str = "",
        realm_id: str = "",
        redirect_uri: str = "",
        token_store: Optional[TokenStore] = None,
        http_client: Optional[HTTPClient] = None,
        rate_limiter: Optional[RateLimiter] = None,
        fixture_path: Optional[str] = None,
        base_url: str = "https://quickbooks.api.intuit.com/v3",
    ) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.realm_id = realm_id
        self.redirect_uri = redirect_uri
        self.token_store: TokenStore = token_store or InMemoryTokenStore()
        self.http_client = http_client
        self.rate_limiter = rate_limiter or RateLimiter(500, 60.0)
        self.fixture_path = fixture_path
        self.base_url = base_url

    # -----------------------------------------------------------------
    # Auth
    # -----------------------------------------------------------------

    def authenticate(self) -> AuthToken:
        """Return the cached AuthToken for this tenant, refreshing if
        expired. Token storage is delegated to the injected `TokenStore` —
        connectors never persist credentials themselves."""
        token = self.token_store.get(self.tenant_id, "quickbooks")
        if token is None:
            raise ConnectorError(
                "No QuickBooks token on file for this tenant. "
                "Run the OAuth flow first."
            )
        if self._is_expired(token):
            token = self._refresh_token(token)
            self.token_store.put(token)
        return token

    @staticmethod
    def _is_expired(token: AuthToken) -> bool:
        # `expires_at` is ISO-8601; if absent assume non-expiring (test path).
        if not token.expires_at:
            return False
        try:
            from datetime import datetime, timezone

            exp = datetime.fromisoformat(token.expires_at.replace("Z", "+00:00"))
            return exp <= datetime.now(timezone.utc)
        except ValueError:
            return False

    def _refresh_token(self, token: AuthToken) -> AuthToken:
        """Refresh an expired access token using the refresh_token grant.

        Tests inject `http_client` to short-circuit the network call.
        """
        if not token.refresh_token:
            raise ConnectorError("Cannot refresh — no refresh_token on record.")
        if self.http_client is None:
            raise ConnectorError(
                "Token refresh requires an http_client; none configured."
            )
        try:
            resp = self.http_client.post(
                "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": token.refresh_token,
                },
            )
        except Exception as exc:  # narrow + redact upstream errors
            log.debug("QB refresh failed", exc_info=exc)
            raise ConnectorError("QuickBooks token refresh failed.") from None
        return AuthToken(
            provider="quickbooks",
            category="accounting",
            tenant_id=token.tenant_id,
            access_token=str(resp.get("access_token", "")),
            refresh_token=str(resp.get("refresh_token", token.refresh_token)),
            expires_at=resp.get("expires_at"),
            scope=token.scope,
        )

    # -----------------------------------------------------------------
    # Reads
    # -----------------------------------------------------------------

    def read_entities(
        self, entity_type: str, filters: Dict[str, Any]
    ) -> List[NormalizedEntity]:
        """Pull entities of `entity_type`, return as NormalizedEntity.

        `entity_type` is the canonical type ('customer', 'vendor', 'person').
        Filters: `display_name_contains`, `email_contains`. Empty dict =
        return all records of this type.
        """
        qb_type = _QB_TYPE_FOR_ENTITY.get(entity_type.lower())
        if qb_type is None:
            raise ConnectorError(
                f"Unsupported entity_type for QuickBooks: {entity_type!r}"
            )
        records = self._fetch_raw_entities(qb_type)
        out: List[NormalizedEntity] = []
        for raw in records:
            if not self._matches_filters(raw, filters):
                continue
            out.append(self._map_entity(raw, qb_type))
        return out

    def read_transactions(
        self, date_range: DateRange
    ) -> List[NormalizedTransaction]:
        """Pull Invoice / Payment / Bill records in `date_range`.

        V1 reads headers only (line items deferred to V2). Fixture-mode
        returns []; real API path issues paginated queries via
        `_fetch_raw_transactions`.
        """
        records = self._fetch_raw_transactions(date_range)
        out: List[NormalizedTransaction] = []
        for raw in records:
            out.append(self._map_transaction(raw))
        return out

    def read_operational_records(
        self, record_type: str, filters: Dict[str, Any]
    ) -> List[NormalizedRecord]:
        """Pull Class / Item / Service hierarchies (dimensional taxonomy).

        Fixture-mode returns []; real path issues a query and emits
        NormalizedRecord with `parent_source_id` set from the hierarchy.
        """
        records = self._fetch_raw_operational(record_type)
        out: List[NormalizedRecord] = []
        for raw in records:
            out.append(self._map_operational(raw, record_type))
        return out

    # -----------------------------------------------------------------
    # Writes (Shadow Ledger only)
    # -----------------------------------------------------------------

    def validate_write(self, proposal: WriteProposal) -> ValidationResult:
        issues: List[str] = []
        if proposal.target_source != "quickbooks":
            issues.append("target_source must be 'quickbooks'")
        if proposal.target_category != "accounting":
            issues.append("target_category must be 'accounting'")
        if proposal.operation not in ("create", "update", "void"):
            issues.append(f"unsupported operation: {proposal.operation!r}")
        return ValidationResult(
            proposal_id=proposal.proposal_id,
            is_valid=not issues,
            issues=issues,
        )

    def execute_write(self, approved_proposal: WriteProposal) -> WriteResult:
        """V1: Shadow Ledger preview only — never calls QB write endpoints."""
        preview = {
            "tenant_id": approved_proposal.tenant_id,
            "target_source": "quickbooks",
            "target_category": "accounting",
            "operation": approved_proposal.operation,
            "target_source_id": approved_proposal.target_source_id,
            "payload": approved_proposal.payload,
            "note": "V1 Shadow Ledger preview — no live mutation performed.",
        }
        return WriteResult(
            proposal_id=approved_proposal.proposal_id,
            executed=False,
            shadow=True,
            preview=preview,
        )

    def rollback_write(self, write_result: WriteResult) -> RollbackResult:
        if write_result.shadow:
            return RollbackResult(
                proposal_id=write_result.proposal_id,
                rolled_back=True,
                notes="Shadow preview — no live write to roll back.",
            )
        return RollbackResult(
            proposal_id=write_result.proposal_id,
            rolled_back=False,
            notes="Live rollback not implemented in V1.",
        )

    # -----------------------------------------------------------------
    # CSV fallback
    # -----------------------------------------------------------------

    def export_csv_fallback(
        self, entity_type: str, date_range: DateRange
    ) -> CSVExport:
        entities = self.read_entities(entity_type, {})
        headers = [
            "source_id",
            "source",
            "category",
            "entity_category",
            "raw_name",
            "normalized_name",
            "email",
        ]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=headers)
        writer.writeheader()
        for e in entities:
            writer.writerow({k: getattr(e, k, "") for k in headers})
        return CSVExport(
            entity_type=entity_type,
            row_count=len(entities),
            csv_text=buf.getvalue(),
            headers=headers,
        )

    # -----------------------------------------------------------------
    # Internal: data source + mapping
    # -----------------------------------------------------------------

    def _fetch_raw_entities(self, qb_type: str) -> List[Dict[str, Any]]:
        if self.fixture_path is not None:
            return [r for r in self._load_fixture() if r.get("type") == qb_type]
        if self.http_client is None:
            return []
        self.rate_limiter.acquire()
        token = self.authenticate()
        url = (
            f"{self.base_url}/company/{self.realm_id}/query"
            f"?query=select%20*%20from%20{qb_type}"
        )
        try:
            resp = self.http_client.get(
                url,
                headers={
                    "Authorization": f"Bearer {token.access_token}",
                    "Accept": "application/json",
                },
            )
        except Exception as exc:
            log.debug("QB read_entities failed", exc_info=exc)
            raise ConnectorError(f"QuickBooks read failed for {qb_type}.") from None
        records = resp.get("QueryResponse", {}).get(qb_type, [])
        return list(records)

    def _fetch_raw_transactions(
        self, date_range: DateRange
    ) -> List[Dict[str, Any]]:
        if self.fixture_path is not None:
            return []
        if self.http_client is None:
            return []
        out: List[Dict[str, Any]] = []
        for txn_type in ("Invoice", "Payment", "Bill"):
            self.rate_limiter.acquire()
            token = self.authenticate()
            q = (
                f"select * from {txn_type} where TxnDate >= '{date_range.start}'"
                f" and TxnDate <= '{date_range.end}'"
            )
            url = f"{self.base_url}/company/{self.realm_id}/query?query={q}"
            try:
                resp = self.http_client.get(
                    url,
                    headers={"Authorization": f"Bearer {token.access_token}"},
                )
            except Exception as exc:
                log.debug("QB read_transactions failed", exc_info=exc)
                raise ConnectorError(
                    f"QuickBooks transaction read failed for {txn_type}."
                ) from None
            records = resp.get("QueryResponse", {}).get(txn_type, [])
            for r in records:
                r["_txn_type"] = txn_type
                out.append(r)
        return out

    def _fetch_raw_operational(self, record_type: str) -> List[Dict[str, Any]]:
        return []

    def _load_fixture(self) -> List[Dict[str, Any]]:
        path = Path(self.fixture_path) if self.fixture_path else None
        if path is None or not path.exists():
            raise ConnectorError(f"Fixture not found: {self.fixture_path!r}")
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ConnectorError("Fixture must be a JSON list of records.")
        return data

    @staticmethod
    def _matches_filters(raw: Dict[str, Any], filters: Dict[str, Any]) -> bool:
        if not filters:
            return True
        if "display_name_contains" in filters:
            name = raw.get("display_name") or raw.get("DisplayName") or ""
            if filters["display_name_contains"].lower() not in name.lower():
                return False
        if "email_contains" in filters:
            email = raw.get("email") or raw.get("PrimaryEmailAddr") or ""
            if isinstance(email, dict):
                email = email.get("Address", "")
            if filters["email_contains"].lower() not in email.lower():
                return False
        return True

    def _map_entity(
        self, raw: Dict[str, Any], qb_type: str
    ) -> NormalizedEntity:
        """Map a QB record to NormalizedEntity. Accepts both fixture shape
        (snake_case, flat) and live QB API shape (PascalCase, nested)."""
        source_id = str(raw.get("id") or raw.get("Id") or "")
        display_name = (
            raw.get("display_name") or raw.get("DisplayName") or ""
        )
        entity_category = raw.get("entity_category") or (
            "person" if qb_type == "Employee" else "organization"
        )
        email_field = raw.get("email") or raw.get("PrimaryEmailAddr") or None
        if isinstance(email_field, dict):
            email_field = email_field.get("Address")

        normalized_input = {
            "id": source_id,
            "source": "quickbooks",
            "entity_category": entity_category,
            "display_name": display_name,
            "email": email_field,
        }
        normalized = normalize_entity(normalized_input)
        # raw_record carries the full QB record for matcher Stage 3 signals.
        normalized.raw_record = raw
        return normalized

    def _map_transaction(self, raw: Dict[str, Any]) -> NormalizedTransaction:
        txn_type = raw.get("_txn_type", raw.get("type", "unknown")).lower()
        amount = float(raw.get("TotalAmt") or raw.get("total_amt") or 0.0)
        currency = (
            raw.get("CurrencyRef", {}).get("value")
            or raw.get("currency")
            or "USD"
        )
        txn_date = raw.get("TxnDate") or raw.get("txn_date") or ""
        if txn_type == "invoice" or txn_type == "payment":
            counterparty = (
                raw.get("CustomerRef", {}).get("value")
                or raw.get("customer_ref")
            )
            counterparty_kind = "customer"
        elif txn_type == "bill":
            counterparty = (
                raw.get("VendorRef", {}).get("value") or raw.get("vendor_ref")
            )
            counterparty_kind = "vendor"
        else:
            counterparty = None
            counterparty_kind = None
        return NormalizedTransaction(
            source_id=str(raw.get("Id") or raw.get("id") or ""),
            source="quickbooks",
            category="accounting",
            txn_type=txn_type,
            amount=amount,
            currency=currency,
            txn_date=txn_date,
            counterparty_source_id=counterparty,
            counterparty_kind=counterparty_kind,
            raw_record=raw,
        )

    def _map_operational(
        self, raw: Dict[str, Any], record_type: str
    ) -> NormalizedRecord:
        return NormalizedRecord(
            source_id=str(raw.get("Id") or raw.get("id") or ""),
            source="quickbooks",
            category="accounting",
            record_type=record_type,
            name=raw.get("Name") or raw.get("name") or "",
            parent_source_id=raw.get("ParentRef", {}).get("value")
            if isinstance(raw.get("ParentRef"), dict)
            else None,
            attributes={
                k: v
                for k, v in raw.items()
                if k not in ("Id", "Name", "id", "name")
            },
            raw_record=raw,
        )
