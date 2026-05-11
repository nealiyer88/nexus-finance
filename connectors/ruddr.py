"""RUDDR connector (category: psa).

V1 second connector. RUDDR is a PSA (Professional Services Automation)
system encoding entity identity via client slugs, project codes
(e.g. "CEN-GENAI-SOW3"), and resource (team-member) records — none of
which share schema with QuickBooks. The cross-category thesis depends on
the matcher resolving these to the same canonical entity as QB's
CustomerRef.

Auth: API key (no OAuth refresh). Write surface: Shadow Ledger preview
only — `execute_write` never calls a RUDDR write endpoint in V1.

Operational records: RUDDR projects are nested under client / vendor
records in the source. `read_operational_records('project', ...)`
flattens these into NormalizedRecord rows whose `parent_source_id` points
to the owning client — that pointer is the cross-category edge candidate
the matcher consumes.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
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


class ConnectorError(RuntimeError):
    """Public-safe wrapper around RUDDR errors. Raw bodies logged at DEBUG."""


# ---------------------------------------------------------------------------
# Token store + rate limiter (same protocols as QB connector for symmetry)
# ---------------------------------------------------------------------------


class TokenStore(Protocol):
    def get(self, tenant_id: str, provider: str) -> Optional[AuthToken]: ...
    def put(self, token: AuthToken) -> None: ...


class InMemoryTokenStore:
    def __init__(self) -> None:
        self._tokens: Dict[tuple[str, str], AuthToken] = {}

    def get(self, tenant_id: str, provider: str) -> Optional[AuthToken]:
        return self._tokens.get((tenant_id, provider))

    def put(self, token: AuthToken) -> None:
        self._tokens[(token.tenant_id, token.provider)] = token


class RateLimiter:
    """Sliding-window rate limiter with injectable clock/sleep for tests."""

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
        now = self._clock()
        cutoff = now - self.window_s
        self._timestamps = [t for t in self._timestamps if t >= cutoff]
        if len(self._timestamps) >= self.max_requests:
            wait = self._timestamps[0] + self.window_s - now
            if wait > 0:
                self._sleep(wait)
        self._timestamps.append(self._clock())

    def backoff(self, attempt: int) -> None:
        self._sleep(self._backoff_base * (2 ** (attempt - 1)))


class HTTPClient(Protocol):
    def get(self, url: str, headers: Dict[str, str]) -> Dict[str, Any]: ...
    def post(
        self, url: str, headers: Dict[str, str], data: Dict[str, Any]
    ) -> Dict[str, Any]: ...


# ---------------------------------------------------------------------------
# RUDDR connector
# ---------------------------------------------------------------------------


_RUDDR_TYPE_FOR_ENTITY: Dict[str, str] = {
    "client": "client",
    "vendor": "vendor",
    "person": "team-member",
    "team-member": "team-member",
    "resource": "team-member",
}


class RUDDRConnector(ConnectorInterface):
    """RUDDR connector. Category: psa."""

    category = "psa"

    def __init__(
        self,
        tenant_id: str,
        api_key: str = "",
        token_store: Optional[TokenStore] = None,
        http_client: Optional[HTTPClient] = None,
        rate_limiter: Optional[RateLimiter] = None,
        fixture_path: Optional[str] = None,
        base_url: str = "https://api.ruddr.io/v1",
    ) -> None:
        self.tenant_id = tenant_id
        self.api_key = api_key
        self.token_store: TokenStore = token_store or InMemoryTokenStore()
        self.http_client = http_client
        self.rate_limiter = rate_limiter or RateLimiter(120, 60.0)
        self.fixture_path = fixture_path
        self.base_url = base_url

    # -----------------------------------------------------------------
    # Auth
    # -----------------------------------------------------------------

    def authenticate(self) -> AuthToken:
        """Return the API key as an AuthToken. RUDDR uses non-expiring
        keys; there is no refresh dance."""
        token = self.token_store.get(self.tenant_id, "ruddr")
        if token is not None:
            return token
        if not self.api_key:
            raise ConnectorError(
                "No RUDDR API key on file for this tenant."
            )
        token = AuthToken(
            provider="ruddr",
            category="psa",
            tenant_id=self.tenant_id,
            access_token=self.api_key,
        )
        self.token_store.put(token)
        return token

    # -----------------------------------------------------------------
    # Reads
    # -----------------------------------------------------------------

    def read_entities(
        self, entity_type: str, filters: Dict[str, Any]
    ) -> List[NormalizedEntity]:
        """Pull RUDDR Client / Vendor / Resource (team-member) records.

        `entity_type` is canonical ('client', 'vendor', 'person').
        Filters: `display_name_contains`, `email_contains`,
        `department`. Empty dict = all records of this type.
        """
        ruddr_type = _RUDDR_TYPE_FOR_ENTITY.get(entity_type.lower())
        if ruddr_type is None:
            raise ConnectorError(
                f"Unsupported entity_type for RUDDR: {entity_type!r}"
            )
        records = self._fetch_raw_entities(ruddr_type)
        out: List[NormalizedEntity] = []
        for raw in records:
            if not self._matches_filters(raw, filters):
                continue
            out.append(self._map_entity(raw, ruddr_type))
        return out

    def read_transactions(
        self, date_range: DateRange
    ) -> List[NormalizedTransaction]:
        """Pull Time Entry records in `date_range`.

        Fixture-mode returns [] (V1 fixtures are entities-only). Real
        API path emits one NormalizedTransaction per time entry with
        `project_code` populated.
        """
        records = self._fetch_raw_time_entries(date_range)
        return [self._map_time_entry(r) for r in records]

    def read_operational_records(
        self, record_type: str, filters: Dict[str, Any]
    ) -> List[NormalizedRecord]:
        """Pull RUDDR Project records.

        Projects in RUDDR fixtures live nested under client/vendor
        records. We flatten them: each project becomes a
        NormalizedRecord with `parent_source_id` set to the owning
        client's RUDDR id. That parent pointer is the project→client
        edge the matcher consumes in Stage 6.
        """
        if record_type.lower() != "project":
            raise ConnectorError(
                f"Unsupported record_type for RUDDR: {record_type!r}"
            )
        out: List[NormalizedRecord] = []
        for parent in self._iter_parents_with_projects():
            parent_id = str(parent.get("id") or parent.get("Id") or "")
            for proj in parent.get("projects") or []:
                if not self._matches_project_filters(proj, filters):
                    continue
                out.append(
                    NormalizedRecord(
                        source_id=f"{parent_id}::{proj.get('code', '')}",
                        source="ruddr",
                        category="psa",
                        record_type="project",
                        name=proj.get("name", ""),
                        parent_source_id=parent_id,
                        attributes={
                            "code": proj.get("code"),
                            "status": proj.get("status"),
                            "budget_hours": proj.get("budget_hours"),
                            "logged_hours": proj.get("logged_hours"),
                            "hourly_rate": proj.get("hourly_rate"),
                            "department": proj.get("department"),
                            "client_source_id": parent_id,
                        },
                        raw_record=proj,
                    )
                )
        return out

    # -----------------------------------------------------------------
    # Writes (Shadow Ledger only)
    # -----------------------------------------------------------------

    def validate_write(self, proposal: WriteProposal) -> ValidationResult:
        issues: List[str] = []
        if proposal.target_source != "ruddr":
            issues.append("target_source must be 'ruddr'")
        if proposal.target_category != "psa":
            issues.append("target_category must be 'psa'")
        if proposal.operation not in ("create", "update", "void"):
            issues.append(f"unsupported operation: {proposal.operation!r}")
        return ValidationResult(
            proposal_id=proposal.proposal_id,
            is_valid=not issues,
            issues=issues,
        )

    def execute_write(self, approved_proposal: WriteProposal) -> WriteResult:
        """V1: Shadow Ledger preview only — never calls RUDDR write endpoints."""
        preview = {
            "tenant_id": approved_proposal.tenant_id,
            "target_source": "ruddr",
            "target_category": "psa",
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

    def _fetch_raw_entities(self, ruddr_type: str) -> List[Dict[str, Any]]:
        if self.fixture_path is not None:
            return [r for r in self._load_fixture() if r.get("type") == ruddr_type]
        if self.http_client is None:
            return []
        self.rate_limiter.acquire()
        token = self.authenticate()
        path = self._endpoint_for_type(ruddr_type)
        try:
            resp = self.http_client.get(
                f"{self.base_url}/{path}",
                headers={
                    "Authorization": f"Bearer {token.access_token}",
                    "Accept": "application/json",
                },
            )
        except Exception as exc:
            log.debug("RUDDR read_entities failed", exc_info=exc)
            raise ConnectorError(
                f"RUDDR read failed for {ruddr_type}."
            ) from None
        return list(resp.get("results", resp.get("data", [])))

    def _fetch_raw_time_entries(
        self, date_range: DateRange
    ) -> List[Dict[str, Any]]:
        if self.fixture_path is not None:
            return []
        if self.http_client is None:
            return []
        self.rate_limiter.acquire()
        token = self.authenticate()
        try:
            resp = self.http_client.get(
                f"{self.base_url}/time-entries"
                f"?from={date_range.start}&to={date_range.end}",
                headers={"Authorization": f"Bearer {token.access_token}"},
            )
        except Exception as exc:
            log.debug("RUDDR read_transactions failed", exc_info=exc)
            raise ConnectorError("RUDDR time-entry read failed.") from None
        return list(resp.get("results", resp.get("data", [])))

    def _iter_parents_with_projects(self) -> List[Dict[str, Any]]:
        if self.fixture_path is not None:
            data = self._load_fixture()
            return [r for r in data if r.get("projects")]
        if self.http_client is None:
            return []
        self.rate_limiter.acquire()
        token = self.authenticate()
        try:
            resp = self.http_client.get(
                f"{self.base_url}/projects",
                headers={"Authorization": f"Bearer {token.access_token}"},
            )
        except Exception as exc:
            log.debug("RUDDR read_operational_records failed", exc_info=exc)
            raise ConnectorError("RUDDR project read failed.") from None
        # Real API returns flat projects; synthesize a single grouped parent
        # per client_id so the flattening logic above still works.
        grouped: Dict[str, Dict[str, Any]] = {}
        for proj in resp.get("results", resp.get("data", [])):
            client_id = proj.get("client_id", "")
            grouped.setdefault(
                client_id, {"id": client_id, "projects": []}
            )["projects"].append(proj)
        return list(grouped.values())

    @staticmethod
    def _endpoint_for_type(ruddr_type: str) -> str:
        return {
            "client": "clients",
            "vendor": "vendors",
            "team-member": "team-members",
        }.get(ruddr_type, ruddr_type)

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
            name = raw.get("display_name") or ""
            if filters["display_name_contains"].lower() not in name.lower():
                return False
        if "email_contains" in filters:
            email = raw.get("email") or ""
            if filters["email_contains"].lower() not in email.lower():
                return False
        if "department" in filters:
            if raw.get("department") != filters["department"]:
                return False
        return True

    @staticmethod
    def _matches_project_filters(
        proj: Dict[str, Any], filters: Dict[str, Any]
    ) -> bool:
        if not filters:
            return True
        if "status" in filters and proj.get("status") != filters["status"]:
            return False
        if "department" in filters and proj.get("department") != filters["department"]:
            return False
        if "code_prefix" in filters:
            code = proj.get("code") or ""
            if not code.startswith(filters["code_prefix"]):
                return False
        return True

    def _map_entity(
        self, raw: Dict[str, Any], ruddr_type: str
    ) -> NormalizedEntity:
        source_id = str(raw.get("id") or raw.get("Id") or "")
        display_name = raw.get("display_name") or raw.get("DisplayName") or ""
        entity_category = raw.get("entity_category") or (
            "person" if ruddr_type == "team-member" else "organization"
        )
        email_field = raw.get("email") or None

        normalized = normalize_entity(
            {
                "id": source_id,
                "source": "ruddr",
                "entity_category": entity_category,
                "display_name": display_name,
                "email": email_field,
            }
        )
        normalized.raw_record = raw
        return normalized

    def _map_time_entry(self, raw: Dict[str, Any]) -> NormalizedTransaction:
        hours = float(raw.get("hours") or 0.0)
        rate = float(raw.get("billing_rate") or raw.get("hourly_rate") or 0.0)
        return NormalizedTransaction(
            source_id=str(raw.get("id") or ""),
            source="ruddr",
            category="psa",
            txn_type="time_entry",
            amount=hours * rate,
            currency=raw.get("currency", "USD"),
            txn_date=raw.get("date", ""),
            counterparty_source_id=raw.get("resource_id"),
            counterparty_kind="resource",
            project_code=raw.get("project_code") or raw.get("project", {}).get("code"),
            raw_record=raw,
        )
