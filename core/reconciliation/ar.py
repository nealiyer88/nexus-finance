"""AR reconciliation (feature 15): RUDDR labor vs. QB invoiced revenue,
per resolved canonical client.

Reads only — no writer. The money rows this module aggregates are
persisted by feature 12a's `core.ingestion.transactions`; this module
never inserts, updates, or deletes a `transactions` row.

Conventions mirror `core.graph.entity_store`: module-level functions,
`conn: sqlite3.Connection` first, `tenant_id: Optional[str] = None`
last. Tenant scope is a SQL predicate on `canonical_entities.tenant_id`
and `transactions.tenant_id` — never RLS, never a session setting. When
`tenant_id` is `None` the query text omits the predicate entirely
(mirroring `count_amount_cooccurrence_periods`'s two-branch idiom), so
a caller that *does* pass a tenant can never execute a statement that
silently lacks the filter.

A `transactions` row attaches to a client either directly via
`transactions.canonical_id`, or — for rows whose `canonical_id` is
still null — via `(source, counterparty_source_id)` joined to
`system_references (source, external_id)`, the same dual join key
feature 12a writes against and Signal B3 reads.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from core.graph.entity_store import AMOUNT_TOLERANCE_CAP, AMOUNT_TOLERANCE_PCT

# Category-pair vocabulary this report reads. "psa" carries RUDDR labor;
# "accounting" carries QB revenue, but only the invoice txn_type counts
# as invoiced revenue — a QB "payment" or "bill" row is not an invoice.
_PSA_CATEGORY: str = "psa"
_ACCOUNTING_CATEGORY: str = "accounting"
_INVOICE_TXN_TYPE: str = "invoice"

_CLIENT_JOIN_CLAUSE: str = """
             t.canonical_id = ce.canonical_id
             OR (
                  t.canonical_id IS NULL
                  AND EXISTS (
                        SELECT 1 FROM system_references AS sr
                         WHERE sr.canonical_id = ce.canonical_id
                           AND sr.source = t.source
                           AND sr.external_id = t.counterparty_source_id
                      )
                )
"""

_AR_REPORT_SQL_NO_TENANT: str = f"""
    SELECT
        ce.canonical_id,
        ce.canonical_name,
        COALESCE(SUM(CASE WHEN t.category = ? THEN t.amount ELSE 0 END), 0) AS labor_total,
        COALESCE(SUM(CASE WHEN t.category = ? AND t.txn_type = ? THEN t.amount ELSE 0 END), 0) AS invoiced_total
      FROM canonical_entities AS ce
      LEFT JOIN transactions AS t
        ON ({_CLIENT_JOIN_CLAUSE})
     WHERE ce.entity_type = 'client'
     GROUP BY ce.canonical_id, ce.canonical_name
     ORDER BY ce.canonical_id
"""

_AR_REPORT_SQL_TENANT: str = f"""
    SELECT
        ce.canonical_id,
        ce.canonical_name,
        COALESCE(SUM(CASE WHEN t.category = ? THEN t.amount ELSE 0 END), 0) AS labor_total,
        COALESCE(SUM(CASE WHEN t.category = ? AND t.txn_type = ? THEN t.amount ELSE 0 END), 0) AS invoiced_total
      FROM canonical_entities AS ce
      LEFT JOIN transactions AS t
        ON ({_CLIENT_JOIN_CLAUSE})
       AND t.tenant_id = ?
     WHERE ce.entity_type = 'client'
       AND ce.tenant_id = ?
     GROUP BY ce.canonical_id, ce.canonical_name
     ORDER BY ce.canonical_id
"""

_DETAIL_SQL_NO_TENANT: str = """
    SELECT t.source, t.category, t.txn_type, t.amount, t.currency,
           t.txn_date, t.period, t.external_source_id,
           t.counterparty_source_id, t.canonical_id, t.tenant_id
      FROM transactions AS t
     WHERE (
             t.canonical_id = ?
             OR (
                  t.canonical_id IS NULL
                  AND EXISTS (
                        SELECT 1 FROM system_references AS sr
                         WHERE sr.canonical_id = ?
                           AND sr.source = t.source
                           AND sr.external_id = t.counterparty_source_id
                      )
                )
           )
     ORDER BY t.txn_date
"""

_DETAIL_SQL_TENANT: str = """
    SELECT t.source, t.category, t.txn_type, t.amount, t.currency,
           t.txn_date, t.period, t.external_source_id,
           t.counterparty_source_id, t.canonical_id, t.tenant_id
      FROM transactions AS t
     WHERE (
             t.canonical_id = ?
             OR (
                  t.canonical_id IS NULL
                  AND EXISTS (
                        SELECT 1 FROM system_references AS sr
                         WHERE sr.canonical_id = ?
                           AND sr.source = t.source
                           AND sr.external_id = t.counterparty_source_id
                      )
                )
           )
       AND t.tenant_id = ?
     ORDER BY t.txn_date
"""

_CANONICAL_LOOKUP_SQL_NO_TENANT: str = """
    SELECT canonical_id, canonical_name FROM canonical_entities WHERE canonical_id = ?
"""

_CANONICAL_LOOKUP_SQL_TENANT: str = """
    SELECT canonical_id, canonical_name FROM canonical_entities
     WHERE canonical_id = ? AND tenant_id = ?
"""


def classify_status(labor_total: float, invoiced_total: float) -> str:
    """Classify one client's reconciliation pair against rules §5's
    tolerance (`min(max(|a|, |b|) * AMOUNT_TOLERANCE_PCT,
    AMOUNT_TOLERANCE_CAP)`), imported — never re-literalled — from
    `core.graph.entity_store`.

    - `"MATCHED"` when `|labor - invoiced|` is within tolerance.
    - `"UNBILLED"` when labor exceeds invoiced beyond tolerance (RUDDR
      hours worked but not yet reflected in a QB invoice — including
      the partial-coverage case where invoiced_total is 0).
    - `"OVERBILLED"` when invoiced exceeds labor beyond tolerance.
    """
    variance = labor_total - invoiced_total
    tolerance = min(
        max(abs(labor_total), abs(invoiced_total)) * AMOUNT_TOLERANCE_PCT,
        AMOUNT_TOLERANCE_CAP,
    )
    if abs(variance) <= tolerance:
        return "MATCHED"
    if variance > tolerance:
        return "UNBILLED"
    return "OVERBILLED"


def get_ar_report(
    conn: sqlite3.Connection,
    tenant_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Per-client AR reconciliation report: RUDDR labor total, QB
    invoiced total, variance, variance %, and status, for every
    `entity_type = 'client'` canonical in scope.

    Single statement — the `LEFT JOIN` resolves each `transactions` row
    to its client either directly (`transactions.canonical_id`) or via
    the `(source, counterparty_source_id)` -> `system_references
    (source, external_id)` fallback, exactly as feature 12a writes
    against and Signal B3 reads.
    """
    if tenant_id is None:
        rows = conn.execute(
            _AR_REPORT_SQL_NO_TENANT,
            (_PSA_CATEGORY, _ACCOUNTING_CATEGORY, _INVOICE_TXN_TYPE),
        ).fetchall()
    else:
        rows = conn.execute(
            _AR_REPORT_SQL_TENANT,
            (
                _PSA_CATEGORY,
                _ACCOUNTING_CATEGORY,
                _INVOICE_TXN_TYPE,
                tenant_id,
                tenant_id,
            ),
        ).fetchall()

    report: list[dict[str, Any]] = []
    for canonical_id, canonical_name, labor_total, invoiced_total in rows:
        labor_total = float(labor_total)
        invoiced_total = float(invoiced_total)
        variance = labor_total - invoiced_total
        denominator = invoiced_total or labor_total
        variance_pct = (variance / denominator * 100.0) if denominator else 0.0
        report.append(
            {
                "canonical_id": canonical_id,
                "canonical_name": canonical_name,
                "labor_total": labor_total,
                "invoiced_total": invoiced_total,
                "variance": variance,
                "variance_pct": variance_pct,
                "status": classify_status(labor_total, invoiced_total),
            }
        )
    return report


def get_ar_detail(
    conn: sqlite3.Connection,
    canonical_id: str,
    tenant_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Per-client line-item detail: RUDDR (`psa`) transactions vs. QB
    (`accounting`) transactions, resolved to `canonical_id` by the same
    direct / `system_references`-fallback join `get_ar_report` uses.

    Returns `None` when `canonical_id` is absent, is not an
    `entity_type = 'client'` canonical, or (when `tenant_id` is given)
    is out of tenant scope.
    """
    if tenant_id is None:
        canonical_row = conn.execute(
            _CANONICAL_LOOKUP_SQL_NO_TENANT, (canonical_id,)
        ).fetchone()
    else:
        canonical_row = conn.execute(
            _CANONICAL_LOOKUP_SQL_TENANT, (canonical_id, tenant_id)
        ).fetchone()
    if canonical_row is None:
        return None

    if tenant_id is None:
        rows = conn.execute(
            _DETAIL_SQL_NO_TENANT, (canonical_id, canonical_id)
        ).fetchall()
    else:
        rows = conn.execute(
            _DETAIL_SQL_TENANT, (canonical_id, canonical_id, tenant_id)
        ).fetchall()

    psa_transactions: list[dict[str, Any]] = []
    accounting_transactions: list[dict[str, Any]] = []
    for (
        source,
        category,
        txn_type,
        amount,
        currency,
        txn_date,
        period,
        external_source_id,
        counterparty_source_id,
        row_canonical_id,
        row_tenant_id,
    ) in rows:
        record = {
            "source": source,
            "category": category,
            "txn_type": txn_type,
            "amount": float(amount),
            "currency": currency,
            "txn_date": txn_date,
            "period": period,
            "external_source_id": external_source_id,
            "counterparty_source_id": counterparty_source_id,
            "canonical_id": row_canonical_id,
            "tenant_id": row_tenant_id,
        }
        if category == _PSA_CATEGORY:
            psa_transactions.append(record)
        elif category == _ACCOUNTING_CATEGORY:
            accounting_transactions.append(record)

    return {
        "canonical_id": canonical_row[0],
        "canonical_name": canonical_row[1],
        "psa_transactions": psa_transactions,
        "accounting_transactions": accounting_transactions,
    }
