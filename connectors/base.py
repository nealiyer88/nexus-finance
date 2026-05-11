"""ConnectorInterface — the V1 contract every source-system connector implements.

Both V1 connectors (QuickBooks Online, RUDDR) and any future connector must
subclass `ConnectorInterface` and declare a `category` class attribute drawn
from `VALID_CATEGORIES`. The interface is intentionally narrow: read-side
methods normalize source records into the shared dataclass shapes, write-side
methods always route through Shadow Ledger preview in V1 — `execute_write`
NEVER performs a live mutation against the source system.

`NormalizedEntity` is re-exported from `core.ingestion.normalizer` (the Stage 0
output dataclass) so that connectors and the matcher share a single
definition. Connectors do not invent their own entity shape.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional

from core.ingestion.normalizer import NormalizedEntity


VALID_CATEGORIES: tuple[str, ...] = (
    "accounting",
    "psa",
    "ap",
    "payments",
    "crm",
    "expense",
    "payroll",
)


# ---------------------------------------------------------------------------
# Shared payload dataclasses
# ---------------------------------------------------------------------------


@dataclass
class AuthToken:
    """OAuth2 / API-key auth artifact returned by `authenticate()`.

    Tokens are encrypted at rest with customer-specific keys, scoped per
    system category. Concrete connectors set `provider`, `category`, and
    `tenant_id` to identify the credential context.
    """

    provider: str                # "quickbooks" | "ruddr"
    category: str                # "accounting" | "psa" | ...
    tenant_id: str
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[str] = None
    scope: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DateRange:
    """Inclusive ISO-8601 (YYYY-MM-DD) date range."""

    start: str
    end: str


@dataclass
class NormalizedTransaction:
    """Output shape for `read_transactions()`.

    Connector-agnostic transaction surface. Source-specific fields are
    preserved in `raw_record` for matcher Stage 3 signal extraction.
    """

    source_id: str
    source: str                  # "quickbooks" | "ruddr"
    category: str                # "accounting" | "psa"
    txn_type: str                # "invoice" | "payment" | "bill" | "time_entry" | ...
    amount: float
    currency: str
    txn_date: str                # ISO-8601
    counterparty_source_id: Optional[str]
    counterparty_kind: Optional[str]   # "customer" | "vendor" | "client" | ...
    project_code: Optional[str] = None
    raw_record: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedRecord:
    """Output shape for `read_operational_records()`.

    Operational records cover non-financial structures: dimensional taxonomy
    (QB Class), project hierarchies (RUDDR Project), service catalogs, etc.
    Records carry typed `attributes` and optional graph hints for
    cross-category edge extraction (e.g. project → client).
    """

    source_id: str
    source: str
    category: str
    record_type: str             # "project" | "class" | "item" | ...
    name: str
    attributes: Dict[str, Any] = field(default_factory=dict)
    parent_source_id: Optional[str] = None
    raw_record: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WriteProposal:
    """Proposed write submitted to `validate_write()` / `execute_write()`."""

    proposal_id: str
    tenant_id: str
    target_source: str
    target_category: str
    operation: str               # "create" | "update" | "void"
    target_source_id: Optional[str]
    payload: Dict[str, Any]


@dataclass
class ValidationResult:
    """Outcome of `validate_write()`."""

    proposal_id: str
    is_valid: bool
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class WriteResult:
    """Outcome of `execute_write()`.

    V1: always a Shadow Ledger preview. `executed=False`, `shadow=True`, and
    `preview` carries the formatted proposed change for human review.
    """

    proposal_id: str
    executed: bool
    shadow: bool
    preview: Dict[str, Any]
    external_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class RollbackResult:
    """Outcome of `rollback_write()`."""

    proposal_id: str
    rolled_back: bool
    notes: Optional[str] = None


@dataclass
class CSVExport:
    """Result of `export_csv_fallback()`."""

    entity_type: str
    row_count: int
    csv_text: str
    headers: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ConnectorInterface
# ---------------------------------------------------------------------------


class ConnectorInterface(ABC):
    """Abstract contract for every V1 source-system connector.

    Subclasses MUST set the `category` class attribute to one of
    `VALID_CATEGORIES`. The base class enforces this via `__init_subclass__`.

    V1 write-path contract: `execute_write` returns a Shadow Ledger preview
    only. Concrete connectors MUST NOT call source-system write endpoints
    from `execute_write`. The class-level `SHADOW_LEDGER_ONLY = True` flag
    documents this and lets callers assert the invariant.
    """

    category: ClassVar[str] = ""
    SHADOW_LEDGER_ONLY: ClassVar[bool] = True

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Allow intermediate abstract subclasses (still ABC) to skip the check.
        if getattr(cls, "__abstractmethods__", None):
            return
        category = getattr(cls, "category", "")
        if category not in VALID_CATEGORIES:
            raise TypeError(
                f"{cls.__name__}.category must be one of {VALID_CATEGORIES!r}; "
                f"got {category!r}"
            )

    @abstractmethod
    def authenticate(self) -> AuthToken:
        """Acquire (and refresh, if needed) credentials for this connector.

        Tokens are encrypted at rest with customer-specific keys, scoped per
        system category. Implementations must not log raw tokens.
        """

    @abstractmethod
    def read_entities(
        self, entity_type: str, filters: Dict[str, Any]
    ) -> List[NormalizedEntity]:
        """Return source-system entities normalized to `NormalizedEntity`.

        `entity_type` is the canonical type ('client', 'vendor', 'person', ...);
        connector implementations translate to source-specific resources
        (e.g. QB Customer → client).
        """

    @abstractmethod
    def read_transactions(
        self, date_range: DateRange
    ) -> List[NormalizedTransaction]:
        """Return source-system transactions in `date_range` as the
        connector-agnostic `NormalizedTransaction` shape."""

    @abstractmethod
    def read_operational_records(
        self, record_type: str, filters: Dict[str, Any]
    ) -> List[NormalizedRecord]:
        """Return non-financial structural records (projects, classes,
        items) as `NormalizedRecord`. Used by the matcher for graph-edge
        extraction (project → client, class → cost_center, ...)."""

    @abstractmethod
    def validate_write(self, proposal: WriteProposal) -> ValidationResult:
        """Validate a proposed write WITHOUT executing it. Returns a
        machine-readable list of issues / warnings."""

    @abstractmethod
    def execute_write(self, approved_proposal: WriteProposal) -> WriteResult:
        """V1: Shadow Ledger preview only. Never performs live mutations.

        Concrete connectors MUST format `approved_proposal` into a
        `WriteResult` with `executed=False`, `shadow=True`, and a populated
        `preview` dict. Live write-back is deferred until post-90-day
        customer approval (see roadmap section: "No write-back" in NOT-SCOPE).
        """

    @abstractmethod
    def rollback_write(self, write_result: WriteResult) -> RollbackResult:
        """Undo a previously executed write. V1 has no live writes, so this
        always returns `rolled_back=True` for shadow previews and is a
        placeholder for the post-V1 write surface."""

    @abstractmethod
    def export_csv_fallback(
        self, entity_type: str, date_range: DateRange
    ) -> CSVExport:
        """Offline CSV export — used when API access is unavailable or for
        customer-side audit / archival."""
