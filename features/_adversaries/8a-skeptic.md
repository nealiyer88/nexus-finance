# Skeptic: Argument AGAINST building feature 8a as specified

## 1. Scope is a Trojan horse disguised as a "retrofit"

This brief calls itself a "retrofit" but bundles **three independent risk surfaces** into one feature:
1. A brand-new embedding subsystem (`embeddings.py` + model fetch + CI dependency story)
2. Stage 2c blocking integration
3. A reconciliation of Signal Set B against unseen shipped code in `scoring.py`

These should not ship together. B-reconciliation has nothing to do with fastText — it's a correctness audit of B1–B6 logged boost behavior. Bundling it with the model-logistics work means a single test failure in either surface blocks both. A 1-file feature became a 6-file diff exactly per the known failure mode the brief itself cites.

Recommend splitting: **8a-embed** (Stage 2c + Signal Set C), then **8b-signalB** (reconciliation, +0.20 cap, audit logging). Each is independently reviewable.

## 2. The "reconcile, not greenfield" instruction will fail

Dependencies §3 says "CC must read the shipped versions and reconcile, not greenfield." This is a known failure mode — agents under instruction to "extend" shipped code routinely rewrite when the existing shape is awkward. The brief gives no fingerprint of what's actually in `scoring.py` today: did feature 8 ship B1–B6 at all? Partial? Stubs? Without that fingerprint inlined, the builder cannot tell "reconcile" from "implement from scratch." Pre-flight should run `git show HEAD:core/matching/scoring.py | head -200` and paste the actual current B-signal surface into the brief, or this turns into greenfield with a reconciliation label.

## 3. Hidden complexity the brief glosses

- **Aliases are embedded too** (Implementation Notes §3): canonical_name + every alias. At ~10 aliases/entity the "flat cosine at <500 canonicals" math becomes 5,000 vectors, and each hit needs a reverse-lookup to its owning `canonical_id`. The brief doesn't specify that data structure.
- **B3 amount co-occurrence and B6 graph neighborhood** require transactional + graph traversal data at scoring time. `scoring.py` extending to read graph state crosses the layer boundary that section 12 of rules pins to `core/graph/entity_store.py`. The brief silently assumes scoring can reach into the graph store.
- **"Exactly capped at +0.20"** as a test assertion is floating-point fragile. `assert total == 0.20` will flake; the brief should say `pytest.approx`.
- **`compress-fasttext` pin is load-bearing** (Complexity rationale acknowledges this) but the resolution is punted to "Phase 1 engineer adversary." That's a process dependency, not a decision. If that step doesn't happen, the builder picks the library blind.

## 4. Wrong timing — the success criterion is untestable in CI

Success criterion: `"pacrim tech" ↔ "pacific rim technologies international"` must score >0.70 in `tests/test_scoring.py`. But Implementation Notes §4 says tests **monkeypatch `embed`** so the suite doesn't need the model. You cannot assert real abbreviation lift with a stubbed embedder. Either CI downloads the ~25–50MB quantized model (CI cost + Apple Silicon risk the brief itself flags), or the headline success criterion is fake. The brief wants both and resolves neither.

## 5. Simpler alternative

Ship Stage 2c fastText recall first as **8a-block-only**: prove the bridge signal surfaces the abbreviation pair as a *candidate*, no scoring change. That alone moves the 80→95 needle on recall. Defer Signal Set C scoring weight + B reconciliation to 8b once the model + CI story is real, not "to be confirmed by adversary debate." 80% of the value, half the diff, none of the cross-layer entanglement.