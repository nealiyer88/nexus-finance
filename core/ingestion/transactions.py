"""Transaction writer (feature 12a): the missing link between a connector's
`read_transactions()` and the shipped `transactions` table.

Nothing else in this repo persists a `NormalizedTransaction`. This module
does exactly that, one connector per call (mirroring
`core.ingestion.pipeline.run_ingestion`'s single-connector stance), and
nothing more: no entity resolution, no fuzzy matching, no alias search.
`canonical_id` is only ever set from an exact `system_references` hit.

Module-level functions only, no class wrapper. `conn: sqlite3.Connection`
is always the first parameter. This module never finalizes the caller's
transaction (no commit, no rollback) — the caller owns the transaction
boundary, exactly as `core/graph/entity_store.py`,
`core/matching/training_data.py` and `core/matching/pending_store.py`
already do. It never opens a connection of its own either; production
callers get one from `core.matching.pending_store.get_connection`.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Optional

from connectors.base import ConnectorInterface, DateRange, NormalizedTransaction

# The RUDDR fixture's client-id key, read only by the counterparty
# fallback below. RUDDR time entries carry the *resource* who logged the
# hours as their immediate counterparty; the receivable is owed by the
# client, so the write layer falls back to this key on `raw_record`.
_RUDDR_CLIENT_ID_KEY: str = "client_id"

# Roles that are NOT the party a receivable is owed by. Only these fall
# back to the client-id key on `raw_record`; every other populated kind
# (QuickBooks "customer" / "vendor") is carried through verbatim.
_FALLBACK_COUNTERPARTY_KINDS: frozenset[str] = frozenset({"resource"})

_PERIOD_RE = re.compile(r"^(\d{4}-\d{2})-\d{2}")


def derive_period(txn_date: str) -> str:
    """Return the `'YYYY-MM'` bucket for `txn_date` (the 8b rule).

    Derived from `txn_date` alone, never from insert/load/clock time.
    Raises `ValueError` when no bucket can be derived — this function
    never substitutes today's date.
    """
    if not txn_date:
        raise ValueError("txn_date is empty; no period can be derived from it")
    match = _PERIOD_RE.match(txn_date)
    if not match:
        raise ValueError(
            f"cannot derive a YYYY-MM period from txn_date={txn_date!r}"
        )
    return match.group(1)


def _derive_counterparty_reference(txn: NormalizedTransaction) -> Optional[str]:
    """Derive the source-side customer/client id `count_amount_cooccurrence_periods`
    filters on. One documented helper, applied uniformly to every connector:

    1. `counterparty_kind` already names the receivable-owing party (QB's
       "customer" / "vendor") -> carry `counterparty_source_id` through verbatim.
    2. `counterparty_kind` names a role that is not that party (RUDDR's
       "resource") -> fall back to the client-id key on `raw_record`.
    3. Neither yields a value -> None. The row is still written, just inert
       for B3 by construction.
    """
    if txn.counterparty_kind is not None and (
        txn.counterparty_kind not in _FALLBACK_COUNTERPARTY_KINDS
    ):
        return txn.counterparty_source_id

    client_ref = txn.raw_record.get(_RUDDR_CLIENT_ID_KEY)
    if client_ref:
        return str(client_ref)
    return None


def _resolve_canonical_id(
    conn: sqlite3.Connection,
    source: str,
    counterparty_reference: Optional[str],
    tenant_id: str,
) -> Optional[str]:
    """Look `(source, counterparty_reference)` up against `system_references`
    (single-valued under its `UNIQUE (source, external_id)`), tenant-scoped by
    joining back to `canonical_entities`. Returns None when nothing resolves —
    no fuzzy matching, no alias search, no entity creation."""
    if not counterparty_reference:
        return None
    row = conn.execute(
        """
        SELECT s.canonical_id
          FROM system_references AS s
          JOIN canonical_entities AS c ON c.canonical_id = s.canonical_id
         WHERE s.source = ? AND s.external_id = ? AND c.tenant_id = ?
        """,
        (source, counterparty_reference, tenant_id),
    ).fetchone()
    return row[0] if row is not None else None


def _existing_txn_id(
    conn: sqlite3.Connection, tenant_id: str, source: str, external_source_id: str
) -> Optional[int]:
    row = conn.execute(
        """
        SELECT txn_id FROM transactions
         WHERE tenant_id = ? AND source = ? AND external_source_id = ?
        """,
        (tenant_id, source, external_source_id),
    ).fetchone()
    return int(row[0]) if row is not None else None


def upsert_transaction(
    conn: sqlite3.Connection, txn: NormalizedTransaction, tenant_id: str
) -> int:
    """Persist one `NormalizedTransaction`, return the `txn_id` of the new
    or pre-existing row.

    Keyed on `(tenant_id, source, external_source_id)`. Existing row ->
    UPDATE the mutable columns in place, preserving `txn_id` and
    `created_at`. No existing row -> INSERT. A delete-and-reinsert upsert
    idiom is never used — SQLite treats NULLs as distinct in a UNIQUE
    index, so a naive upsert on a nullable `tenant_id` would silently
    duplicate on every run; requiring a non-empty `tenant_id` sidesteps
    that hazard entirely.

    Raises `ValueError` for an empty `tenant_id`, an empty `txn.source_id`,
    or a `txn.txn_date` from which no period derives.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required and must be non-empty")
    if not txn.source_id:
        raise ValueError("txn.source_id is empty; nothing to key the row on")

    period = derive_period(txn.txn_date)
    counterparty_reference = _derive_counterparty_reference(txn)
    canonical_id = _resolve_canonical_id(
        conn, txn.source, counterparty_reference, tenant_id
    )

    existing_id = _existing_txn_id(conn, tenant_id, txn.source, txn.source_id)
    if existing_id is not None:
        conn.execute(
            """
            UPDATE transactions
               SET category = ?, txn_type = ?, amount = ?, currency = ?,
                   txn_date = ?, period = ?, counterparty_source_id = ?,
                   canonical_id = ?
             WHERE txn_id = ?
            """,
            (
                txn.category,
                txn.txn_type,
                txn.amount,
                txn.currency,
                txn.txn_date,
                period,
                counterparty_reference,
                canonical_id,
                existing_id,
            ),
        )
        return existing_id

    cur = conn.execute(
        """
        INSERT INTO transactions (
            tenant_id, source, category, external_source_id, txn_type,
            amount, currency, txn_date, period, counterparty_source_id,
            canonical_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tenant_id,
            txn.source,
            txn.category,
            txn.source_id,
            txn.txn_type,
            txn.amount,
            txn.currency,
            txn.txn_date,
            period,
            counterparty_reference,
            canonical_id,
        ),
    )
    return int(cur.lastrowid)


@dataclass(frozen=True)
class TransactionIngestionSummary:
    """Bucket accounting over one `ingest_transactions` run. Buckets are
    mutually exclusive and exhaustive: `read_total == inserted + updated +
    skipped`, mirroring the discipline `IngestionSummary` already states."""

    read_total: int
    inserted: int
    updated: int
    skipped: int


def ingest_transactions(
    connector: ConnectorInterface,
    conn: sqlite3.Connection,
    tenant_id: str,
    date_range: DateRange,
) -> TransactionIngestionSummary:
    """Call `connector.read_transactions(date_range)` and upsert every
    element. One connector per call, mirroring `run_ingestion`.

    A transaction is skipped (and counted in `skipped`) only when it
    cannot satisfy the shipped `NOT NULL` columns: an empty `source_id`
    or a `txn_date` from which no period derives. Both guards raise
    `ValueError` from `upsert_transaction`, caught here on that specific
    type — a broad catch-all would pass vacuously on empty input and
    hide exactly the mapping bugs this feature exists to surface.

    Raises `ValueError` for an empty `tenant_id`.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required and must be non-empty")

    records = connector.read_transactions(date_range)

    inserted = 0
    updated = 0
    skipped = 0
    for txn in records:
        pre_existing_id = (
            _existing_txn_id(conn, tenant_id, txn.source, txn.source_id)
            if txn.source_id
            else None
        )
        try:
            upsert_transaction(conn, txn, tenant_id)
        except ValueError:
            skipped += 1
            continue
        if pre_existing_id is not None:
            updated += 1
        else:
            inserted += 1

    return TransactionIngestionSummary(
        read_total=len(records),
        inserted=inserted,
        updated=updated,
        skipped=skipped,
    )
