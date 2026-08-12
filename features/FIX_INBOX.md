# Fix Inbox — quick captures awaiting triage

Capture a defect the moment you notice it — one sanitized line under `## Untriaged`
(no sensitive identifiers, amounts, or PII). Later, expand each into a fix brief
(`templates/FIX_BRIEF_TEMPLATE.md` → `features/fixes/FX-<n>-<slug>.md`) + a
`FIX_QUEUE.md` row, then check the item off with `→ FX-N`. Prune items older than
30 days at triage.

Rule of thumb at triage: **default a fix's Class to PATCH when unsure — never TWEAK**
(under-gating is the dangerous direction). If one shipped feature accumulates ≥2
fixes, that's a recurring-defect signal: a gate let something through — find out
which and tighten it.

## Untriaged
- [ ] {YYYY-MM-DD HH:MM} · {one-liner}

## Triaged
