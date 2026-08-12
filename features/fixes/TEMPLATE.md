# Fix Brief: {Title}

**ID:** FX-{n}
**Class:** {TWEAK | PATCH | MAPPING | AMENDMENT}
**Module:** {one word naming the subsystem — drives the `fix(<module>):` commit scope}
**Feature:** {source shipped feature ID this fix belongs to, e.g. B17}

> **Class semantics** (drives the gate matrix in `rocket.sh --fix`):
> - **TWEAK** — cosmetic, UI-only. QA review SKIPPED; code review + exercise gate still
>   run. Requires `FIX_UI_PATHS_RE` in rocket.config.sh — without it (or with any diff
>   path outside it) the escalator upgrades TWEAK→PATCH automatically.
> - **PATCH** — logic bug, wrong behavior. Full gates.
> - **MAPPING** — data/backend mapping error. QB validation gate FORCED (when QB_GATE=1).
> - **AMENDMENT** — the contract/architecture itself must change. The reality check runs
>   first (GO/FLAG; FLAG blocks the fix for human review).
>
> **Default PATCH when unsure — never default TWEAK** (under-gating is the dangerous
> direction). The post-build diff escalator only ever ADDS gates; it never removes one.
<!-- Optional: <!-- qb-validation: required --> or <!-- qb-validation: skip --> -->

---

## DEFECT
{Observed vs expected, repro steps. Aggregate-only — no sensitive identifiers,
amounts, or PII.}

## ROOT CAUSE (if known)
{file:line if identified; "unknown" is fine — the fix build investigates}

## FILES
**Modify:** `{paths}`
**DO NOT MODIFY:** `{boundary — everything outside the defect's blast radius}`

## Success Criteria
<!-- Each criterion MUST be verifiable with a grep, test, or structural check. -->
1. {the defect is gone: structurally verifiable check}
2. No regression: the project test command passes vs the recorded baseline
3. Exercise gate PASS (all classes run Phase 4c)

## NON-GOALS
{Hard boundaries — a fix fixes; it does not refactor or extend.}
