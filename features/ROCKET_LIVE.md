# Rocket Live Log

> **Real-time run commentary.** `rocket.sh`'s `narrate()` function appends one block per phase as it runs. Each entry is one timestamp + slug + label, followed by a 2–3 sentence plain-English summary from `MODEL_NARRATE` (cheap Haiku). Tail this file in a separate terminal to follow the build live:
>
> ```bash
> tail -f features/ROCKET_LIVE.md
> ```
>
> **Aggregate-only rule.** Per CLAUDE.md, this file contains no client names, dollar amounts, invoice numbers, or doc identifiers. The `narrate` instruction passed to MODEL_NARRATE enforces this constraint inline.
>
> **Lifecycle.** Each `rocket.sh` invocation appends; nothing is truncated. The file grows indefinitely. Sweep on a major prune cycle if needed.

---

### Run starting 2026-06-20 — features 8a → 10 → 11

Pre-flight cleanup (this branch): queue row 8 flipped to SHIPPED; row 9 SHIPPED via PR #7. Next features: **8a fasttext-signal-retrofit** (v4 retrofit of 7+8, hard dep for 12), **10 resolution-graph-update**, **11 approval-queue**. MAX_FEATURES=3.

Watch for: model tiering (Opus debate / Sonnet build / Haiku narrate), per-feature $50 budget cap, blind-critic QA reviewer, repo-mutation guards.

### 2026-07-02T02:24:37 · 8A · 🛡️ code-review mutated the repo out of lane — hard-reverted to 2f28b9e8. Reviewers/gates are read-only.

### 2026-07-02T02:24:37 · 8A · 🔍 qa review running

### Run starting 2026-07-05T22:12:44 — features 8b → 10 → 11 (skill-driven, interactive session)

8a merged to main via PR #11 earlier tonight. This run processes the three unblocked queue rows in dependency order: **8b b3-transactions-table-and-amount-signal** (completes Signal Set B), **10 resolution-graph-update** (Stage 6), **11 approval-queue** (dashboard). Feature 11 branches from 10's tip (hard dependency). Heartbeat entries per phase below.

### 2026-07-05T22:12:44 · 8B · 🥊 adversary debate running (design / skeptic / engineer)

### 2026-07-05T22:17:50 · 8B · 🥊 debate reconciled → hardened design committed (build-as-one won; join keys, month periods, max-anchored symmetric tolerance pinned). 📝 prompt generated. 🔨 build starting

### 2026-07-05T22:23:59 · 8B · 🚀 Starting 8b (b3-transactions-table-and-amount-signal.md)

### 2026-07-05T22:24:26 · 8B · 🎭 design agent weighed in
The team approved building a transactions table (SQL + SQLite versions) to add the final missing piece of their entity-matching algorithm — specifically, a signal that checks whether two potential matches have similar transaction amounts in the same time period. This unblocks the measurement of whether their auto-approval threshold works correctly and closes out a deferred piece from the previous build step. The table design is deliberately minimal (just source system, entity reference, amount, and period) to keep complexity contained and avoid pulling in transaction details that don't help with matching.

### 2026-07-05T22:25:01 · 8B · 🎭 skeptic agent weighed in
The skeptic argues that the 8b feature proposes a database table and a matching signal that depends on it, but leaves out the connector integration that would actually populate the table with real transaction data. Result: the table ships empty in production, the signal never fires, and the downstream feature it's supposed to enable (12) can't use it. The argument also flags several underspecified technical details (how to bucket transactions by time period, which amounts drive tolerance checks, dual schema maintenance, tenant isolation testing) and proposes two alternatives: either fold ingestion into 8b and accept larger scope, or defer the table entirely and compute the signal on-the-fly from data already in memory during matching. The core claim is that 8b as drafted builds the middle piece of a three-piece feature and calls it done.

### 2026-07-05T22:26:17 · 8B · 🎭 engineer agent weighed in
The engineer reviewed whether feature 8b (adding a transaction-matching signal) can be built as specified and confirmed it's technically feasible, but the requirements brief has three fuzzy spots that need to be nailed down before building — mainly clarifying what "same period" means for matching transactions, which amount to compare when checking tolerance, and how to look up unmatched candidates by their transaction IDs. The real build effort is 2–3 sessions rather than the originally estimated 1, and the engineer flagged a test-writing pitfall where a developer might accidentally pass tests without checking that the new signal survives the system's overall scoring cap.

### 2026-07-05T22:27:03 · 8B · ⚖️ Reconciled the debate into a hardened plan
The team added a new matching signal that recognizes when two entities from different systems had transaction amounts that align within a 2% tolerance in the same month—a strong clue they're the same real-world client or vendor. The database design is complete and tested across both database engines, but the actual data ingestion from source systems is deferred to a separate feature, so in this build phase the signal only fires against test fixtures. A later feature will wire up live transaction syncing and measure whether this signal meaningfully improves the matching accuracy.

### 2026-08-22T08:38:42 · 8B · 🚀 Starting 8b (b3-transactions-table-and-amount-signal.md)

### 2026-08-22T08:38:42 · 8B · 🔎 reality check running (brief vs current repo)

### 2026-08-22T08:41:14 · 8B · 🚩 reality check FLAG — brief vs repo mismatch; feature BLOCKED for human review
The reality check found three major problems with the feature brief: it assumes database security patterns that don't exist in the codebase, it references an amount-comparison rule that's undefined in code, and it lacks concrete success criteria needed to verify the work is done correctly. These mismatches between the brief and the actual codebase need to be resolved with clear decisions before the team starts building.

### 2026-08-22T08:41:24 · 10 · 🚀 Starting 10 (resolution-graph-update.md)

### 2026-08-22T08:41:24 · 10 · 🔎 reality check running (brief vs current repo)

### 2026-08-22T08:43:40 · 10 · 🚩 reality check FLAG — brief vs repo mismatch; feature BLOCKED for human review
The feature is ready in concept but blocked by four concrete issues: the database doesn't have the tables it needs for storing approvals and audit trails, there's a mismatch in how tenant data is handled between test and production schemas, the test command won't actually run in this repo, and column names need alignment. All fixable, but they need explicit decisions before the team can start building.

### 2026-08-22T08:43:49 · 16 · 🚀 Starting 16 (connectors-audit-infra.md)

### 2026-08-22T08:43:49 · 16 · 🔎 reality check running (brief vs current repo)

### 2026-08-22T08:46:25 · 16 · 🚩 reality check FLAG — brief vs repo mismatch; feature BLOCKED for human review
The build flagged feature 16 as blocked: it assumes database infrastructure (Supabase auth and tenant isolation policies) that doesn't exist yet, and the team needs to decide upfront whether those are in scope or handled separately. Additionally, five success criteria can't be verified as written—missing a Dash app to render pages, test tables that don't exist in the SQLite schema, and a mismatched test-runner command.
