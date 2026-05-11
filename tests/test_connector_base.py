"""Tests for the V1 ConnectorInterface contract."""

from __future__ import annotations

import inspect
from typing import Any, Dict, List

import pytest

from connectors.base import (
    AuthToken,
    ConnectorInterface,
    CSVExport,
    DateRange,
    NormalizedEntity,
    NormalizedRecord,
    NormalizedTransaction,
    RollbackResult,
    VALID_CATEGORIES,
    ValidationResult,
    WriteProposal,
    WriteResult,
)


def test_connector_interface_cannot_be_instantiated() -> None:
    """ABC with abstract methods cannot be instantiated directly."""
    with pytest.raises(TypeError):
        ConnectorInterface()  # type: ignore[abstract]


def test_invalid_category_rejected_at_subclass_time() -> None:
    """A concrete subclass with an invalid category fails at class-create."""
    with pytest.raises(TypeError):

        class BogusConnector(ConnectorInterface):
            category = "not-a-real-category"

            def authenticate(self) -> AuthToken: ...
            def read_entities(self, entity_type: str, filters: Dict[str, Any]) -> List[NormalizedEntity]: ...
            def read_transactions(self, date_range: DateRange) -> List[NormalizedTransaction]: ...
            def read_operational_records(self, record_type: str, filters: Dict[str, Any]) -> List[NormalizedRecord]: ...
            def validate_write(self, proposal: WriteProposal) -> ValidationResult: ...
            def execute_write(self, approved_proposal: WriteProposal) -> WriteResult: ...
            def rollback_write(self, write_result: WriteResult) -> RollbackResult: ...
            def export_csv_fallback(self, entity_type: str, date_range: DateRange) -> CSVExport: ...


def test_valid_subclass_instantiates() -> None:
    """A complete subclass with a valid category and all methods implemented
    instantiates successfully."""

    class StubConnector(ConnectorInterface):
        category = "accounting"

        def authenticate(self) -> AuthToken:
            return AuthToken(
                provider="stub",
                category=self.category,
                tenant_id="t",
                access_token="x",
            )

        def read_entities(
            self, entity_type: str, filters: Dict[str, Any]
        ) -> List[NormalizedEntity]:
            return []

        def read_transactions(
            self, date_range: DateRange
        ) -> List[NormalizedTransaction]:
            return []

        def read_operational_records(
            self, record_type: str, filters: Dict[str, Any]
        ) -> List[NormalizedRecord]:
            return []

        def validate_write(self, proposal: WriteProposal) -> ValidationResult:
            return ValidationResult(proposal_id=proposal.proposal_id, is_valid=True)

        def execute_write(self, approved_proposal: WriteProposal) -> WriteResult:
            return WriteResult(
                proposal_id=approved_proposal.proposal_id,
                executed=False,
                shadow=True,
                preview={"op": approved_proposal.operation},
            )

        def rollback_write(self, write_result: WriteResult) -> RollbackResult:
            return RollbackResult(proposal_id=write_result.proposal_id, rolled_back=True)

        def export_csv_fallback(
            self, entity_type: str, date_range: DateRange
        ) -> CSVExport:
            return CSVExport(entity_type=entity_type, row_count=0, csv_text="", headers=[])

    inst = StubConnector()
    assert inst.category == "accounting"
    assert inst.SHADOW_LEDGER_ONLY is True


def test_shadow_ledger_docstring_present() -> None:
    """execute_write docstring must state the V1 Shadow Ledger constraint."""
    doc = ConnectorInterface.execute_write.__doc__ or ""
    assert "Shadow Ledger preview only" in doc
    assert "Never performs live mutations" in doc


def test_all_abstract_methods_have_type_hints() -> None:
    """Every abstract method declares a return annotation and typed params."""
    methods = [
        "authenticate",
        "read_entities",
        "read_transactions",
        "read_operational_records",
        "validate_write",
        "execute_write",
        "rollback_write",
        "export_csv_fallback",
    ]
    for name in methods:
        fn = getattr(ConnectorInterface, name)
        sig = inspect.signature(fn)
        assert sig.return_annotation is not inspect.Signature.empty, (
            f"{name} missing return annotation"
        )
        for pname, param in sig.parameters.items():
            if pname == "self":
                continue
            assert param.annotation is not inspect.Signature.empty, (
                f"{name}({pname}) missing annotation"
            )


def test_dataclasses_importable_and_constructible() -> None:
    """All payload dataclasses are importable and constructible."""
    AuthToken(provider="quickbooks", category="accounting", tenant_id="t", access_token="x")
    DateRange(start="2026-01-01", end="2026-01-31")
    NormalizedTransaction(
        source_id="x", source="quickbooks", category="accounting",
        txn_type="invoice", amount=1.0, currency="USD", txn_date="2026-01-01",
        counterparty_source_id=None, counterparty_kind=None,
    )
    NormalizedRecord(
        source_id="x", source="quickbooks", category="accounting",
        record_type="class", name="A.B.C",
    )
    WriteProposal(
        proposal_id="p1", tenant_id="t", target_source="quickbooks",
        target_category="accounting", operation="create",
        target_source_id=None, payload={},
    )
    ValidationResult(proposal_id="p1", is_valid=True)
    WriteResult(proposal_id="p1", executed=False, shadow=True, preview={})
    RollbackResult(proposal_id="p1", rolled_back=True)
    CSVExport(entity_type="customer", row_count=0, csv_text="", headers=[])


def test_valid_categories_match_spec() -> None:
    """VALID_CATEGORIES is the V1 category vocabulary."""
    assert set(VALID_CATEGORIES) == {
        "accounting", "psa", "ap", "payments", "crm", "expense", "payroll",
    }


def test_normalized_entity_is_normalizer_dataclass() -> None:
    """`NormalizedEntity` re-exported from connectors.base IS the dataclass
    produced by Stage 0 normalizer — single source of truth."""
    from core.ingestion.normalizer import NormalizedEntity as NEFromNormalizer

    assert NormalizedEntity is NEFromNormalizer
