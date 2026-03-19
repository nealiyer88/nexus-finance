# Nexus Finance

Cross-category financial entity resolution platform for professional services firms.

## What This Is

Nexus Finance resolves entity identity across structurally incompatible financial system categories. QuickBooks (accounting) calls a client "Cenlar, LLC." RUDDR (PSA/labor) calls the same client "cenlar-fsb" with project code "CEN-GENAI-SOW3." No system knows these are the same entity. Nexus Finance does.

## Status

V1 build — pre-alpha. QB + RUDDR connectors. Shadow Ledger only (read-only, no write-back).

## Quick Start

```bash
cp .env.example .env
# Fill in credentials
pip install -r requirements.txt
uvicorn api.main:app --reload
```

## Generate Test Data

```bash
python scripts/generate_test_data.py
```

Produces synthetic cross-category test data in `tests/fixtures/`.

## Architecture

See `.claude/rules/01-nexus-finance-v1.md` for V1 scope, schemas, and architectural principles.
