"""Connector package.

Public surface is the V1 `ConnectorInterface` contract plus the dataclasses
that flow across it. Concrete connectors (quickbooks, ruddr) implement the
interface and live alongside this file.
"""

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
    VALID_CATEGORIES,
    WriteProposal,
    WriteResult,
)

__all__ = [
    "AuthToken",
    "ConnectorInterface",
    "CSVExport",
    "DateRange",
    "NormalizedEntity",
    "NormalizedRecord",
    "NormalizedTransaction",
    "RollbackResult",
    "ValidationResult",
    "VALID_CATEGORIES",
    "WriteProposal",
    "WriteResult",
]
