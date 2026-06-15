"""In-memory inverted indices for Pipeline Stage 2 (Blocking).

Two index types:

- `TokenIndex`  : whitespace-token  -> set of canonical_ids
- `NgramIndex`  : character trigram -> set of canonical_ids (sentinel-padded)

Both are built once per pipeline invocation from the current SQLite state
(`canonical_entities.canonical_name` plus all `entity_aliases.value` rows).
There is no incremental update path in V1 — Stage 6 owns the write path,
which will trigger a rebuild. At <50K canonicals per tenant the rebuild
cost is sub-millisecond.

Indices contain no rapidfuzz / phonetic-library calls. Stage 3 will layer
those on top of the candidates these indices produce.
"""

from __future__ import annotations

import sqlite3
from typing import ClassVar, Iterable, Optional


def _tokenize(value: str) -> list[str]:
    return [t for t in value.split() if t]


class TokenIndex:
    """Whitespace-token to set of canonical_ids."""

    def __init__(self) -> None:
        self._by_token: dict[str, set[str]] = {}

    @classmethod
    def build(
        cls,
        conn: sqlite3.Connection,
        tenant_id: Optional[str] = None,
    ) -> "TokenIndex":
        idx = cls()
        for canonical_id, value in _iter_seed_strings(conn, tenant_id):
            for tok in _tokenize(value):
                idx._by_token.setdefault(tok, set()).add(canonical_id)
        return idx

    def lookup(self, tokens: Iterable[str]) -> set[str]:
        out: set[str] = set()
        for tok in tokens:
            hits = self._by_token.get(tok)
            if hits:
                out.update(hits)
        return out

    def candidates_per_token(self, tokens: Iterable[str]) -> dict[str, set[str]]:
        """Return token -> set of canonical_ids for each token that hit.

        Used by Stage 2 to attach per-candidate blocking_signals.
        """
        out: dict[str, set[str]] = {}
        for tok in tokens:
            hits = self._by_token.get(tok)
            if hits:
                out[tok] = set(hits)
        return out


class NgramIndex:
    """Character-trigram (sentinel-padded) to set of canonical_ids.

    Padding wraps each indexed/queried string with `^` and `$`, ensuring
    short names (`"ibm"`, `"hp"`) generate non-empty trigram sets.
    """

    N: ClassVar[int] = 3
    PAD_LEFT: ClassVar[str] = "^"
    PAD_RIGHT: ClassVar[str] = "$"

    def __init__(self) -> None:
        self._by_gram: dict[str, set[str]] = {}

    @classmethod
    def build(
        cls,
        conn: sqlite3.Connection,
        tenant_id: Optional[str] = None,
    ) -> "NgramIndex":
        idx = cls()
        for canonical_id, value in _iter_seed_strings(conn, tenant_id):
            for gram in cls.trigrams(value):
                idx._by_gram.setdefault(gram, set()).add(canonical_id)
        return idx

    def lookup(self, query: str) -> set[str]:
        if not query or not query.strip():
            return set()
        out: set[str] = set()
        for gram in self.trigrams(query):
            hits = self._by_gram.get(gram)
            if hits:
                out.update(hits)
        return out

    def candidates_per_gram(self, query: str) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        if not query or not query.strip():
            return out
        for gram in self.trigrams(query):
            hits = self._by_gram.get(gram)
            if hits:
                out[gram] = set(hits)
        return out

    @staticmethod
    def trigrams(s: str) -> tuple[str, ...]:
        if not s:
            return ()
        padded = NgramIndex.PAD_LEFT + s + NgramIndex.PAD_RIGHT
        n = NgramIndex.N
        return tuple(padded[i : i + n] for i in range(len(padded) - n + 1))


def _iter_seed_strings(
    conn: sqlite3.Connection,
    tenant_id: Optional[str],
) -> Iterable[tuple[str, str]]:
    """Yield (canonical_id, value) for every canonical_name and alias under
    the tenant filter. Values are assumed to already be in normalized form."""
    if tenant_id is None:
        for cid, name in conn.execute(
            "SELECT canonical_id, canonical_name FROM canonical_entities"
        ):
            if name:
                yield cid, name
        for cid, value in conn.execute(
            "SELECT canonical_id, value FROM entity_aliases"
        ):
            if value:
                yield cid, value
    else:
        for cid, name in conn.execute(
            """
            SELECT canonical_id, canonical_name
              FROM canonical_entities
             WHERE tenant_id = ?
            """,
            (tenant_id,),
        ):
            if name:
                yield cid, name
        for cid, value in conn.execute(
            """
            SELECT a.canonical_id, a.value
              FROM entity_aliases AS a
              JOIN canonical_entities AS c ON c.canonical_id = a.canonical_id
             WHERE c.tenant_id = ?
            """,
            (tenant_id,),
        ):
            if value:
                yield cid, value
