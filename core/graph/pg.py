"""Postgres connection helper for the (currently offline) Postgres path.

This module is importable with no database present and never opens a
connection at import time. `DATABASE_URL` is read lazily, and its absence
means "Postgres not available" — never an error. Feature 10c wires the
Stage 6 call site and the writers on top of `is_available()` / `connect()`;
this module only owns the connection primitive.

`DATABASE_URL` is a live credential at all times, whether or not its value
happens to carry a password. It is never logged, never printed, and never
included in an error message — `connect()` may name the *variable*, never
its *value*.
"""

from __future__ import annotations

import os
from typing import Optional

import psycopg

# Explicit connect timeout (seconds), overridable via environment variable.
# Never an inline numeric literal at any `connect()` call site.
PG_CONNECT_TIMEOUT_SECONDS: int = int(os.environ.get("PG_CONNECT_TIMEOUT_SECONDS", "5"))

# Fixed bootstrap tenant id — a literal, never generated. Feature 16 binds
# `DEFAULT_TENANT_ID = BOOTSTRAP_TENANT_ID` and asserts identity; do not
# rename, relocate, database-couple, or compute this value.
BOOTSTRAP_TENANT_ID: str = "00000000-0000-0000-0000-000000000000"


def get_dsn() -> Optional[str]:
    """Return the configured Postgres DSN, or None if unset.

    Loads `.env` first via the project's existing lazy, guarded import
    convention (see `core.matching.llm_fallback`) so a missing
    `python-dotenv` install does not break importability. Never raises,
    never invents a default DSN.
    """
    if os.environ.get("DATABASE_URL") is None:
        try:
            from dotenv import load_dotenv as _load_dotenv

            _load_dotenv()
        except ImportError:
            pass

    return os.environ.get("DATABASE_URL") or None


def is_available() -> bool:
    """Return True when a Postgres DSN is configured.

    The single availability helper — used by test skip guards, and by
    feature 10c's Stage 6 call site, which reaches Postgres through
    nothing else.
    """
    return get_dsn() is not None


def connect() -> "psycopg.Connection":
    """Open a Postgres connection using the configured DSN.

    Raises `RuntimeError` naming the `DATABASE_URL` variable (never its
    value) when no DSN is configured. Driver exceptions that embed
    connection parameters are caught and re-raised with the message
    scrubbed.
    """
    dsn = get_dsn()
    if dsn is None:
        raise RuntimeError("DATABASE_URL is not set; Postgres is not available")

    try:
        return psycopg.connect(dsn, connect_timeout=PG_CONNECT_TIMEOUT_SECONDS)
    except psycopg.Error as exc:
        raise RuntimeError(
            f"failed to connect to Postgres (see DATABASE_URL): {type(exc).__name__}"
        ) from None
