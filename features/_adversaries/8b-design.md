# Design Advocate: B3 Transactions Table + Amount Co-occurrence Signal

## 1. Why this design is correct

The brief solves a concrete, load-bearing problem: **Signal B3 is the only Signal Set B signal blocked on missing schema**, and Signal Set B is explicitly enumerated in `.claude/rules/01-nexus-finance-v1.md` §1 as B1–B6. Shipping 8a with "B1/B2/B4/B5/B6 shipped, B3 deferred" is a documented compromise, not a design — 8b closes it. The scoring path in `core/matching/scoring.py::_compute_b_boosts` already assumes a six-signal vocabulary participating in the +0.20 cap; leaving B3 as a permanent stub means the cap is theoretical and the `signal_breakdown` telemetry is structurally incomplete.

The choice of a **minimal transactions table** — source system, category, entity ref, amount, period — is exactly the surface area §5 requires to evaluate `AMOUNT_TOLERANCE = min(TotalAmt * 0.02, $500)` in the same period. Not less (you can't compute co-occurrence without amount + period + entity), not more (invoice line items, tax splits, memo fields, FX rates are not signal inputs). Adding both `db/schema.sql` and `db/schema_sqlite.sql` respects the existing two-dialect pattern; skipping the SQLite dialect would immediately break local dev and the graph-store rule in §11 ("SQLite + edge tables sufficient at <50K nodes V1").

Gating B3 to the **0.70–0.90 ambiguous zone** is the correct restraint. Below 0.70, other signals dominate and the pair routes to review anyway (§5); above 0.90, it auto-approves and the boost is wasted compute. This mirrors how B1/B2/B4–B6 already behave in 8a and preserves interpretability of `signal_breakdown`.

## 2. Why the scope is right

The brief is deliberately narrow: **one table, one signal, tests**. It resists three tempting expansions that would each blow up complexity:

- **Connector wiring** (`read_transactions` on QB/RUDDR) is explicitly out of scope — correctly, because that's a full connector task with OAuth, pagination, and normalization surface, not a scoring feature.
- **Retuning the other five B-signals** is out of scope — 8a shipped them; touching them now re-litigates settled tuning.
- **Transaction ingestion pipeline / Stage 6 graph updates** are untouched — 8b is a Stage 3 signal, and adding Stage 6 wiring would drag in cluster conflict detection.

M-complexity is honest: one table × two dialects + one boost function branch + tests. Anything larger would be dishonest scoping; anything smaller would omit the SQLite dialect and break local test runs.

## 3. Why now

Feature 12 is called out in the brief as needing B3 for **95% gate measurement**. Without B3, the gate measurement is measuring a five-signal engine and calling it six-signal — the metric is invalid. 8a is SHIPPED (dependency clean), the CC-LEARNINGS entry from 2026-06-21 already diagnosed the deadlock from attempting B3 without its data dependency, and this brief inverts that failure mode by leading with the schema. The dependency chain is: **8b unblocks 12 unblocks the auto-match gate claim in §1**.

## 4. Risks of NOT building this

- **Signal Set B remains permanently five-signal**, contradicting §1 and §6 Stage 3.
- **The +0.20 cap is untestable** at its real ceiling — no test can exercise all six signals firing.
- **Feature 12's 95% measurement is invalid** and either slips or ships against a partial engine.
- **The CC-LEARNINGS deadlock repeats** the next time someone tries B3 without scaffolding the table first.

Build it as specified.