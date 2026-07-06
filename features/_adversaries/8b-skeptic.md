# Skeptic: Argument Against Building 8b As Specified

## 1. Scope is deceptively wrong — "M" is a lie without ingestion

The brief ships a transactions table *and* a signal that consumes it, but explicitly punts "transaction ingestion from connectors" as out-of-scope-unless-trivial. That's the entire load-bearing question, hand-waved. Without ingestion, B3 evaluates against a table that is empty in every real tenant. You will ship a signal that provably fires only in `test_scoring.py`. Feature 12's 95% gate — which this brief names as the reason 8b exists — cannot be measured against an empty table. So either (a) ingestion IS in scope and this is L, not M; or (b) ingestion is deferred and 8b delivers zero production signal until a third feature lands. Pick one honestly before build.

## 2. Hidden complexity the brief glosses

- **"Period" is undefined.** Is it QB's txn date? RUDDR's time-entry week? Fiscal period? Calendar month? QB invoices and RUDDR time entries land on different date semantics — an invoice dated 2026-03-31 for February work co-occurs with February time entries, not March. Period bucketing is the *actual* algorithm here, and the brief treats it as a column.
- **AMOUNT_TOLERANCE is defined on `TotalAmt`** (rules §5) — but co-occurrence is between two transactions with two amounts. Which amount drives the tolerance? The smaller? Larger? Mean? This is not a schema question, it is a scoring definition, and 8a's abbreviation-rescue postmortem shows what happens when scoring semantics are underspecified (QA-005).
- **Two schema dialects** (`schema.sql` + `schema_sqlite.sql`) means every column type, index, and constraint has to be verified in both — the brief says "one new table" as if that's one unit of work.
- **RLS scoping** is trivial to write and non-trivial to test — is there a test harness for tenant isolation on new tables, or does 8b have to build one?
- **Cap interaction with 8a's shipped B-signals.** B3 is +0.10–0.15 into a +0.20 hard cap already saturated in many cases by B1/B2/B4/B5/B6. If B3 is almost always clipped, the signal is measurable in unit tests but invisible in production — again, no contribution to the 95% gate.

## 3. Wrong timing

Feature 12 needs the *measured* effect of B3, which needs *populated* transactions, which needs *connector ingestion*. Building the table + signal without ingestion is building the middle third of a three-part change and calling it shippable. The correct sequence is: define period semantics → ship ingestion for at least QB → then B3 lands with real data on day one. Otherwise 8b is a scaffolding PR pretending to be a feature.

## 4. Simpler alternative — 80% of the value

Defer the table. Add B3 as a **transient signal** computed from whatever the pipeline already has in memory during Stage 3 — if both candidates carry a recent transaction reference (even from connector payloads in the scoring context), evaluate co-occurrence inline. No schema migration, no dual dialects, no RLS surface, no empty-table problem. If measurement later demands persistence, add the table *with* ingestion in one coherent feature.

## Bottom line

8b as written ships the least-useful third of a three-part change, under-defines "period" and tolerance-basis, and names feature 12 as its justification while structurally guaranteeing it can't help feature 12 until ingestion also ships. Rescope: either fold ingestion in (accept L), or defer the table and compute B3 transiently.