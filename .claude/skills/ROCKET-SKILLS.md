# Skills

Ten skills the loop invokes around the deterministic gate runner. Each lives at
`skills/<name>/SKILL.md` with YAML frontmatter (`name`, `description`) and a body stating
its tier, its exact input, its exact output format, and its hard prohibitions.

`setup.sh` / `install.sh` install these to your project's `.claude/skills/`, where they
become slash commands (`/gate-runner`, `/morning-summary`, …). **A name your project
already uses is never opened**: yours is left exactly as it is, ours lands as
`.claude/skills/rocket-<name>/` with its frontmatter `name:` rewritten to match, and the
installer says so. Losing a name is recoverable; losing your skill is not. Skills the
installer placed are tracked in `.claude/.rocket-manifest`, so an upgrade refreshes them —
and keeps any one you have edited, same as an agent.

| Skill | Tier | Job |
|---|---|---|
| `gate-runner` | worker | Runs `gates.sh` for a scope; returns verdict, per-gate status, counts, and log **paths** — never log contents. |
| `lint-fixer` | worker | Mechanical lint and format remediation only; reports anything needing restructuring instead of doing it. |
| `dedup-scout` | worker | Post-wave scan for helpers two parallel units built independently. Reports pairs; refactors nothing. |
| `morning-summary` | worker | What shipped, what blocked, what it cost. One screen, blocked items first. |
| `doc-ritual` | worker | Appends the run log, build record, and learnings entries after a feature finishes. |
| `test-author` | lead | Writes the missing tests when the coverage gate fails — pinning behavior that matters, never padding the percentage. |
| `security-triage` | lead | Real vulnerability vs. noise in secrets and dep-audit output; unproven reachability blocks. |
| `integrator` | lead | Serial merge of unit branches, conflict resolution, full gate suite after **every** merge. |
| `triager` | lead | Classifies a BLOCKED unit as harness artifact, defect, spec error, or scope error; requeues, re-scopes, or escalates. |
| `ci-author` | lead | Generates the CI workflow from the project's configured gate suite so CI and local run the same `gates.sh`. |

**Worker tier** (`MODEL_WORKER`) is for provably mechanical work: fixed input format, fixed
output format, no domain judgment. **Lead tier** (`MODEL_BUILD`) is for anything where a
wrong call ships a defect or discards someone's work — reachability, conflict intent, test
selection, cause classification. When in doubt, a skill is lead; under-spending on judgment
is the expensive mistake.

## Division of labor

`gates.sh` decides pass/fail. It is deterministic bash, it runs the project's own
configured commands, and it emits one `GATE` line per gate plus an exact `VERDICT:` line.
These skills sit *around* that decision: they interpret its output, remediate what it
found, and report what happened — but **a skill must never be the thing that decides a
gate passed.** A skill that concludes "the failure looks cosmetic, calling it green" has
replaced a deterministic check with a language model's opinion, which is precisely the
substitution the harness exists to prevent. Every skill that reports a verdict inherits the
same house rule from `gates.sh`: **NOT-RUN != PASS.** Unable to run, unable to parse, unable
to prove — all report failure. Silence is never evidence of success. Skills are headless:
none may ask a question, and anything unresolvable becomes a flagged line in the output.
