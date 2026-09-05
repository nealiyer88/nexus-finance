# the fix-fleet build

**Reference name: the fix-fleet build.** Next-session trigger: "run the fix-fleet build".

Plan type: PRODUCT pass (`/army-plan product`). Date: 2026-09-04. Repo: `nexus-finance`.
Source of record: `FIX_LIST.md` (29 items, compiled 2026-09-03, all observed in the shipped
tree or in a live run).

This plan queues 14 features, IDs 18–31, closing 24 of the 29 FIX_LIST items. The remaining
5 are excluded with reasons in "Explicitly excluded". Nothing in this document builds
anything; it defines what gets built and in what order.

---

## Decisions locked

Recorded verbatim, Neal, 2026-09-04.

1. **Scope: everything except the learned model.** The XGBoost pairwise classifier (item 7)
   and fine-tuned embeddings (item 8) are DEFERRED — with no accuracy measurement and 91
   fixture records, a trained model would be fitted on almost nothing and unmeasurable.
   They stay on the list, unqueued.
2. **Parallel fan-out: ENABLED, with map approval required.** Up to 3 units per feature,
   `REQUIRE_MAP_APPROVAL=1`, each ownership map approved via `./rocket.sh approve <slug>`
   before fan-out runs.
3. **Live connectors: NO.** Stay on fixtures and expand them (item 13 excluded). Real
   credentials are a separate future decision.
4. **Feature 17 signup/onboarding: PARKED, stays BLOCKED.** Needs commitments on identity
   provider, customer credential encryption/storage, and billing. Nothing in this plan
   depends on it.

---

## Executor

The executor is the rocket-loop harness, **Executor A**. Verified facts about it, current
as of 2026-09-04:

- **Ownership-map schema.** `.claude/agents/sizer.md` is the single source of truth for the
  ownership-map schema; maps are written to `features/_plan/<slug>.units.yml`. This plan
  deliberately does **not** restate the schema. The sizing pass will emit maps against it.
- **Map validation.** `scripts/rocket_schedule.py` validates co-ownership overlap,
  contract-violation (a non-`contract` unit owning a contract file), `depends_on`
  referencing unknown unit ids, plus cycle detection. Flags: `--plan-dir` (default
  `features/_plan`), `--max-parallel`, `--json`, `--emit-plan`. `./rocket.sh schedule` is
  the read-only wrapper — it spawns no agents and costs nothing.
- **Fan-out has never run here.** `features/_plan/` exists but contains only `.gitkeep` —
  zero ownership maps have ever been written in this repo. Fan-out is a capability this
  project has never exercised. See "Risks".
- **Concurrency and approval.** `MAX_PARALLEL` defaults to 3.
  `APPROVED_DIR="features/_plan/.approved"` holds `<slug>.sha256` records binding a human
  approval to the map's exact bytes. After sizing, `rocket.sh` renames the map to
  `<lowercased feature id>.units.yml`.
- **Cross-feature co-scheduling** requires ALL of: both features have approved maps with
  non-empty `owns`; both carry `failure_policy: ship-rest`; neither appears in the other's
  `Depends On`; and their `owns` sets are disjoint.
- **Queue parser.** `FEATURE_QUEUE.md` columns are `# | Feature | Brief | Depends On |
  Status | Spec`. `FEATURE_ID_REGEX='[0-9]+[a-z]?'` (set in `rocket.config.sh`). A row is
  selected only when its Status field reads QUEUED. **There is no numeric sort** —
  `get_next_feature` walks rows in FILE ORDER and takes the first QUEUED row whose deps are
  all SHIPPED. Build order is controlled by physical row position, not by ID.
- **Brief template.** `features/TEMPLATE.md` — DO NOT MODIFY. Required headings: Problem
  Statement; Scope (In Scope / Out of Scope); Success Criteria; Dependencies; Estimated
  Complexity; PROJECT CONTEXT with its subsections.
- **Phase 1 gate.** `.claude/agents/reality-check.md`, validating on 5 axes: contracts exist
  as claimed; dependencies real (checked by content, not presence); nothing already done; no
  stale paths or module names; every success criterion mechanically runnable with `TEST_CMD`
  from `rocket.config.sh` (`.venv/bin/python -m pytest tests/ -x --tb=short`). Verdict line
  is exactly `VERDICT: GO` or `VERDICT: FLAG`.

---

## Features

14 features, IDs 18–31. The existing queue tops out at 17. Listed in dependency order.

### 18 — `matching-accuracy-harness` — Size L
Closes: **item 1**.
A scored evaluation over `tests/fixtures/canonical_ground_truth.json` that reports
precision, recall, F1 and a false-merge count, broken out per category pair, runnable as a
single command and asserted in CI against a floor. Today nothing in the tree measures
accuracy: the closest existing assertion is a bottom-cutoff floor over ground-truth pairs,
and the "auto-match rate" on the overview page counts decisions made, not decisions correct.
**THIS IS THE KEYSTONE** — items 9, 10, 22, 23 and 29 are unmeasurable without it, and every
downstream matching change is unfalsifiable until it lands.
Deps: 8a, 8b, 12.

### 19 — `decision-reasoning-persistence` — Size M
Closes: **item 2**.
`MatchResult.reasoning_trace` is built in `core/matching/engine.py` and handed to Stage 6,
but its only durable home is a Postgres-only table written solely inside `if
pg_is_available()`. On the default SQLite path — the only real engine in V1 — it is
discarded, so auto-approved matches (the majority) have no stored account of why they
merged. Persist the trace on the SQLite path for EVERY disposition, including auto-approve
and no-match, not only inside the Postgres branch.
Deps: 10, 10b, 12.

### 20 — `match-explanation-ui` — Size M
Closes: **item 3**.
`dashboard/pages/approval_queue.py::build_detail_view` renders `dataclasses.asdict` of the
signal breakdown and graph evidence as raw dict text — internal field names on a page, not
an explanation. Replace it with a readable panel: which signals fired, what each
contributed, what the graph corroboration was, and what would have changed the outcome.
Reads the persisted trace from 19.
Deps: 11, 16, 19.

### 21 — `training-corpus-capture` — Size L
Closes: **items 4, 5, 6**.
`core/matching/training_data.py::store_training_pair` writes only when the pair went through
the Stage 5 LLM fallback (the row requires an existing source call id), so auto-approves and
no-matches produce nothing; the table has zero readers anywhere in `core/`, `api/`,
`dashboard/` or `scripts/`; and hard negatives — the ranked alternatives a human passed over
— are not collected at all. Ungate the write so every human decision and every automatic
disposition is captured; collect hard negatives; add a read path so the corpus is
inspectable and exportable rather than write-only. **Redaction and leak-check discipline
must be preserved exactly** — `core/matching/redaction.py` and the `leak_check` calls in
`core/matching/llm_fallback.py` are load-bearing.
Deps: 9, 10b, 19.

### 22 — `threshold-calibration` — Size M
Closes: **items 9, 10**.
`AUTO_APPROVE_THRESHOLD = 0.90`, `SURFACE_THRESHOLD = 0.70` and `LLM_FALLBACK_THRESHOLD =
0.50` are module constants in `core/matching/disposition.py`, derived from nothing. Derive
the cutoffs from measured outcomes using the harness from 18, and produce a signal-ablation
report answering directly whether `fasttext_cosine` earns its 0.05/0.12 weight — the premise
that justified the fastText mandate was measured false in the 8a build record.
Deps: 18.

### 23 — `blocking-channel-fairness` — Size S
Closes: **item 11**.
`EmbeddingIndex.query` returns top_k=50 regardless of cosine quality, and `blocking.py`
evicts embed-only candidates first when the 50-candidate cap is hit. Under a full candidate
set the embedding channel can therefore contribute nothing while still appearing wired. Fix
the eviction policy and prove the change with the accuracy harness — this feature is not
done on a code change alone, it is done when 18 shows the delta.
Deps: 18.

### 24 — `runtime-entrypoints` — Size M
Closes: **items 12, 14, 23**.
Nothing in `api/`, `dashboard/` or `scripts/` calls `run_ingestion` or `ingest_transactions`
— both work when called directly and are called only by tests. There is no button, no
schedule, no CLI. Ship a CLI plus a dashboard trigger; a bootstrap command that enforces the
load-bearing seed order (history first, then reference data, then transactions — the reverse
raises `ValueError: system_reference ... already bound` and yields zero pending decisions);
and the missing module-level `server = app.server` WSGI export in `dashboard/app.py`,
without which a production deployment fails on the missing line.
Deps: 12a, 13, 16.

### 25 — `test-credibility-repair` — Size M
Closes: **item 15**.
Repair the specifically named vacuous tests: feature 14's page tests that assert a Flask GET
returns 200 while Dash routes client-side (the shell returns 200 regardless and no callback
fires); feature 13's `aliases_by_category` isinstance loop whose body executes zero times
when the dict is empty, with no non-emptiness precondition; feature 15's substring-only
tenant-predicate check, satisfiable by merely selecting the column while the loophole stays
live in the detail SQL; `test_build_sidebar_is_pure`, which compares `repr()` of two calls
and which any function passes; and the missing audit-worker queue-full test. **Every
repaired test must be mutation-proved** — shown to fail against the defect it claims to
catch.
Deps: 13, 14, 15, 16.

### 26 — `integration-tier-coverage` — Size M
Closes: **item 16**.
Features 12a, 13, 14 and 15 each added zero integration-marked tests; the tier has been flat
at 35 executed since feature 16 while the suite grew 543 → 639. Add integration coverage for
the paths that have only ever been proven by hand in a session: real ingestion writing rows,
callbacks returning data against a populated store, and first run against an unprovisioned
store.
Deps: 10c, 12a, 15.

### 27 — `legacy-qa-audit` — Size M
Closes: **items 17, 18**.
Features 1–9 shipped under a QA gate that always reported pass and has never been audited;
`GATE_DEBT.md` holds 6 unreviewed entries. Audit both. Every finding is either fixed or
explicitly accepted in writing — "unknown what it missed" is not an acceptable end state.
Deps: none. This feature is dependency-free and can be co-scheduled freely.

### 28 — `dashboard-page-completion` — Size M
Closes: **items 20, 21, 22**.
`dashboard/pages/audit_log.py` registers zero callbacks, so its table is never populated —
the page exists, the feature does not. `dashboard/pages/connectors.py` hardcodes an empty
connector list behind "Coming Soon" cards. `dashboard/pages/ar_reconciliation.py` labels
zero-activity clients `MATCHED`, but zero versus zero is silence, not agreement, and needs a
distinct no-activity state. **THREE DISJOINT PAGE FILES — the natural fan-out candidate.**
Deps: 14, 15, 16, 12a.

### 29 — `fixture-corpus-expansion` — Size L
Closes: **items 24, 25**.
Grow beyond 46+45 entities and 6+4 transactions to cover what is entirely absent: non-English
and non-Latin names; duplicates within a single source; one-to-many and many-to-many merges
(every ground-truth entity today is a clean 1:1 pair); records with missing or empty names;
entities that change over time; multi-tenant collisions; and enough transaction volume to
actually exercise Signal B3 (amount co-occurrence), which is barely exercised at 10 rows.
`tests/fixtures/canonical_ground_truth.json` must be extended in lockstep — new fixtures
without matching ground truth make 18 report a worse number for no real reason.
Deps: 18, 8a, 8b.

### 30 — `tenant-slug-uniqueness` — Size S
Closes: **item 26**.
`tenants.slug` carries a UNIQUE constraint on a name-derived slug, so two customers with the
same name cannot both exist. Fix the uniqueness model.
Deps: 10a, 10c.

### 31 — `design-system` — Size L
Closes: **item 19**.
The dashboard is default Dash components with no design system, no spacing scale and no type
scale — functional and hard to look at. **Deliberately LAST**: design once the content is
settled, so the system is applied to finished pages rather than to pages that 20 and 28 are
still changing.
Deps: 20, 28.

---

## Explicitly excluded

Five of the 29 items are not queued. Reasons, one per item.

- **Items 7 and 8 — XGBoost pairwise classifier, fine-tuned embeddings.** Deferred by
  decision 1. Note this is deferral in line with the spec, not a cut: the v3 spec itself
  scopes both to V2+, and `.claude/rules/01-nexus-finance-v1.md` §11 puts fine-tuning
  explicitly out of V1 scope (re-evaluate at ~20+ customers) and XGBoost likewise. They stay
  on the FIX_LIST, unqueued. Item 7 also notes `scikit-learn` is not in `requirements.txt` —
  any learned model needs that dependency added deliberately.
- **Item 13 — live connector credentials.** Excluded by decision 3. Both connectors stay in
  fixture mode; item 29 expands the fixtures instead. Real credentials are a separate future
  decision.
- **Item 27 — 2,488 test rows in the Postgres `audit_log`.** Operational cleanup, not a
  feature. Deletion is blocked by the tooling safety classifier and must be run by hand:
  ```
  psql -d nexus_finance -c "DELETE FROM audit_log; DELETE FROM approval_decisions;"
  ```
- **Item 28 — feature 17 signup/onboarding.** Parked by decision 4; row 17 stays BLOCKED.
  Needs decisions on identity provider, customer credential encryption and storage, and
  billing — all expensive to reverse after real customers exist. Nothing in this plan
  depends on it.
- **Item 29 — the reality-check gate has no memory between rounds.** An upstream rocket-loop
  harness issue, not a nexus-finance feature. It re-derives the full criteria list each round
  and flags a different subset rather than tracking what it already cleared; observed
  clearing an item explicitly in one round and flagging the same unchanged text in the next,
  and once asserting something the code contradicted. It cost several rounds of rework in the
  2026-09-03 session. This belongs in a Rocket-infra sync: `CLAUDE.md` forbids editing
  `.claude/agents/` as a side effect of feature work.

**Coverage: 23 of 29 items closed by features 18–31; 6 excluded above (7, 8, 13, 27, 28, 29); 0 unaccounted for.**
Items 7 and 8 are a single deferral decision but count as two items.

---

## Build order and why

`get_next_feature` walks `FEATURE_QUEUE.md` rows in FILE ORDER and takes the first QUEUED row
whose dependencies are all SHIPPED. There is no numeric sort. Therefore **physical row
position is the build order control**, and the rows must be inserted in exactly this
sequence, appended after row 17:

```
27  legacy-qa-audit
18  matching-accuracy-harness
19  decision-reasoning-persistence
23  blocking-channel-fairness
22  threshold-calibration
24  runtime-entrypoints
25  test-credibility-repair
26  integration-tier-coverage
30  tenant-slug-uniqueness
28  dashboard-page-completion
21  training-corpus-capture
20  match-explanation-ui
29  fixture-corpus-expansion
31  design-system
```

Rationale for the order, not the IDs:

- **27 first** because it has no dependencies, it is an audit of already-shipped ground, and
  its findings may change what the later features are building on. Discovering a
  features-1–9 defect after 18 has set an accuracy floor is the expensive ordering.
- **18 second** because it is the keystone. 22, 23 and 29 all end in "prove it with the
  harness", and 20 and 21 are hard to judge without it.
- **19 before 20 and 21** — both consume the persisted trace.
- **23 before 22** — fix the eviction unfairness before calibrating thresholds, or the
  calibration is fitted to a channel that is being silently starved.
- **25 and 26 before the page work** so the suite can be trusted to catch what 28 breaks.
- **28 late but before 31** — it is the fan-out pilot (see Risks) and it settles the page
  content that 31 styles.
- **29 late** because expanding fixtures moves every number 18 reports; do it once the
  matching changes have landed, not in the middle of them.
- **31 last**, by design.

**Dependencies are enforced independently of row order.** Each row's `Depends On` column is
checked against SHIPPED status before selection, so a mis-ordered row cannot build early — it
is simply skipped until its deps land. Row order decides which of several *eligible* rows is
taken first. The order above is chosen so that the eligible set is almost always a single
row, which keeps the build deterministic.

---

## Risks

1. **Fan-out has never run in this repository.** `features/_plan/` contains only `.gitkeep`;
   zero ownership maps have ever been written, and `features/_plan/.approved/` does not yet
   exist. Every part of the parallel path — sizer output, `./rocket.sh approve`, the
   scheduler's overlap and contract-violation checks, the concurrent write fences — is
   unexercised in this repo. Stating that plainly: **the first fan-out is itself a test of
   the harness, not just of the feature.** The first fan-out should therefore be a feature
   with genuinely disjoint scopes, and **28 is the safest first candidate** — three separate
   page files under `dashboard/pages/` with no shared surface. **Not 18.** 18 is the keystone,
   it is L, and a harness failure on it stalls nine downstream features. Run 18 on the
   single-writer path.
2. **Brief/repo drift is the dominant historical failure mode.** Rounds of rework in the last
   session came from briefs citing paths, module names or symbols that did not exist as
   claimed — `.claude/rules/01-nexus-finance-v1.md` §12 still marks `core/matching/engine.py`
   as `[PLANNED]` although the file has existed since feature 12 shipped. **Every brief in
   this plan must cite only paths and symbols verified to exist at authoring time**, and the
   reality-check gate checks dependencies by content, not by presence. Where a path is
   uncertain, describe the thing by name rather than asserting a path.
3. **Feature 21 touches redaction and leak-check code.** `core/matching/redaction.py` and the
   `leak_check` calls in `core/matching/llm_fallback.py` are the only thing standing between
   customer data and a stored corpus. Ungating `store_training_pair` multiplies the volume
   flowing through that path by a large factor. A mistake here leaks PII into a persisted
   corpus, which is not a bug you can fix forward by deleting a row. 21 gets the strictest
   verification of any feature in this plan and must not be fanned out across the redaction
   boundary.
4. **Item 29's gate flakiness is still live.** The reality-check gate is excluded from this
   plan as an upstream issue, which means the fix-fleet build runs *with* it. Expect a round
   or two of re-flagged, already-cleared criteria per feature and budget for it rather than
   re-litigating each one.
5. **29 moves the scoreboard.** Expanding the fixture corpus will almost certainly lower the
   precision/recall numbers 18 reports, because the new cases are the hard ones. That is the
   correct outcome, not a regression. The CI floor asserted by 18 must be re-baselined as
   part of 29, deliberately and in the same change, or 29 lands red.

---

## Verification

Exact commands.

```
.venv/bin/python -m pytest tests/ -x --tb=short      # full suite (TEST_CMD)
./rocket.sh schedule                                  # ownership-map validation, read-only
```

The `python -m` form is **required**. Bare `pytest` is not on PATH, and `.venv/bin/pytest`
fails to put the repo root on `sys.path`, so every `from core...` / `from connectors...`
import in `tests/` fails to collect.

`./rocket.sh schedule` spawns no agents and costs nothing — run it freely.

**Current baseline: 639 passed, 35 integration-marked executed.** Any feature that leaves the
integration count at 35 has not moved item 16.

**Each feature must be verified by RUNNING it, not by reading its tests.** This is not
boilerplate: the 2026-09-03 session shipped three green-but-broken features, including an
approval queue that returned 500 on every real page load with 577 tests passing. A green
suite is a precondition for acceptance, never the evidence for it. For each feature, the
acceptance evidence is an actual execution — the CLI invoked, the page loaded, the row
written, the harness run and its number recorded.

---

## Documentation ritual at landing

When the fix-fleet build lands, all four of these happen:

1. **Build record appended to this plan file** — actual per-agent models used, token cost,
   wall-clock, and the verifier panel's findings. Append, do not rewrite the plan above.
2. **`PLAN_LOG.md` status flipped** from `[PLANNED]` to `[SHIPPED]` on this plan's line.
3. **`.claude/rules/01-nexus-finance-v1.md` updated** wherever a rule actually changed —
   at minimum the §12 `[PLANNED]` marker on `core/matching/engine.py`, and §5's threshold
   values if 22 moves them.
4. **`PROMPT_LOG.md` entry** for the run. Append-only, per `CLAUDE.md`.

---

## Next step

**This is the PRODUCT pass. Nothing is sized and nothing builds.**

The sizing pass (`/army-plan build`) is the next action. It:

1. Emits one `features/_plan/<slug>.units.yml` per approved feature, written against the
   schema in `.claude/agents/sizer.md` — that file is the single source of truth for the map
   shape and is not restated here.
2. Validates every emitted map with `./rocket.sh schedule` **before any human review** — a
   map that fails co-ownership, contract-violation, unknown-`depends_on` or cycle checks is
   not worth a human's attention.
3. Presents each surviving map for approval via `./rocket.sh approve <slug>`, which records
   the map's exact bytes in `features/_plan/.approved/<slug>.sha256`.

Only after that: queue rows are inserted in the file order given above, and **nothing builds
without an explicit go.**
