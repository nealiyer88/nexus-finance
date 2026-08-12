# Fix Queue — post-ship fixes (rocket --fix lane)

**Managed by:** `rocket.sh --fix` — it picks the next QUEUED fix whose dependencies are
SHIPPED. Fixes never auto-run: launch batches manually (`./rocket.sh --fix --max N`).

| # | Fix | Brief | Depends On | Status | Class |
|---|-----|-------|------------|--------|-------|

<!--
Column ORDER is load-bearing — rocket.sh parses: # | Fix | Brief | Depends On | Status,
and reads the trailing Class column separately for the pre-flight drift check.
Do NOT reorder columns.

- #          : fix ID matching FIX_ID_REGEX (default FX-1, FX-2, …).
- Brief      : path to the fix brief (features/fixes/FX-<n>-<slug>.md, from
               templates/FIX_BRIEF_TEMPLATE.md).
- Depends On : feature IDs and/or FX-N that must be SHIPPED first (resolved across
               this file AND the feature queue), or — / - for none. Usually the
               source feature the fix belongs to.
- Status     : QUEUED | SHIPPED | BLOCKED | DEFERRED (rocket flips QUEUED→SHIPPED/BLOCKED).
- Class      : TWEAK | PATCH | MAPPING | AMENDMENT — display only; the brief's
               `**Class:**` line is authoritative and a mismatch HALTS at pre-flight.
-->
