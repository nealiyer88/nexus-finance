# RESUME — rocket run 8b→10→11 parked 2026-07-05 ~22:30 (delete when run completes)

## Paste into next session
```
Resume the parked rocket run: read RESUME_ROCKET.md, then relaunch rocket.sh in the background on this branch.
```

## State
- Branch: `rocket-run-8b-10-11` (from main @ c27de7b, 8a merged). Pushed to origin.
- Run stopped manually (usage budget) mid-8b, Phase 2 (prompt-gen) just starting.
- 8b Phase 1 COMPLETE and committed: rocket's own debate at `features/_adversaries/8b-{design,skeptic,engineer,hardened}.md` (~$0.90 spent).
  Also committed: my earlier manual debate/prompt at `features/_adversaries/b3-transactions-table-and-amount-signal.md` + `features/_prompts/b3-transactions-table-and-amount-signal.cc-prompt.md` — richer on join-key/period semantics; reconcile agrees. Forensics only for rocket, but READ the hardened designs before/if building manually.
- Queue: 8b/10/11 still QUEUED (rocket flips status only at ship).

## Resume
1. `git checkout rocket-run-8b-10-11`, clean tree required.
2. `bash rocket.sh --max 3 > /tmp/rocket-8b-10-11.log 2>&1 &` — rocket is idempotent per feature but will REDO 8b Phase 1 (it doesn't checkpoint mid-feature). If you want to skip the ~$1 redo, run `bash rocket.sh --feature 8b` is NOT resumable either — just accept the redo, or build 8b manually from the committed hardened design + prompt and let rocket start at feature 10 (`--max 2` after flipping 8b SHIPPED).
3. Watch: `tail -f features/ROCKET_LIVE.md`. Budget: $50/feature cap, opus plan tier / sonnet build tier.
