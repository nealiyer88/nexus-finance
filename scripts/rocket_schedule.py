#!/usr/bin/env python3
"""rocket_schedule.py — do-no-harm scheduler for the Rocket-Loop fan-out build.

Reads the per-feature ownership maps (features/_plan/<slug>.units.yml), validates the
two hard rules (§4.3 of plans/rocket-loop-fanout-design.md) — disjoint + complete
ownership — and computes the concurrency plan the future parallel executor WOULD run,
WITHOUT running anything concurrently. This is step 2 of the fan-out build order:
prove the maps parse and the partitioning is correct, at zero risk. Execution in
rocket.sh remains strictly sequential; this module only reads and reports.

Portable-to-core candidate (design §9). Stdlib ONLY — no PyYAML, no third-party deps
(decision: stdlib mini-parser). It parses the small, regular YAML subset our maps use
and FAILS LOUD (MiniYAMLError / ScheduleError) on anything it does not recognise,
rather than silently mis-reading an ownership set and corrupting a future merge.
"""

from __future__ import annotations

import argparse
import glob
import itertools
import json
import os
import sys
from dataclasses import dataclass, field


# ── Errors (both are fail-loud; the harness treats a non-zero exit as a block) ──
class MiniYAMLError(ValueError):
    """The map is not in the regular subset we can safely parse."""


class ScheduleError(ValueError):
    """The map parses but is not a valid, schedulable ownership plan."""


# ─────────────────────────────────────────────────────────────────────────────
# 1. Minimal YAML-subset loader
#
# Supports exactly what the maps use: nested mappings, block sequences (`- item`),
# inline flow sequences (`[a, b]` / `[]`), double/single-quoted scalars, plain
# scalars with trailing `# comments`, block scalars (`>` / `|`, whose content we
# keep but never schedule on), and booleans. Tabs are rejected. Anything outside
# this subset raises MiniYAMLError.
# ─────────────────────────────────────────────────────────────────────────────
def _tokenize(text):
    """-> list of (indent, content, lineno), skipping blank and full-line comments."""
    toks = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw:
            raise MiniYAMLError(f"line {lineno}: tab character (YAML forbids tabs)")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        toks.append((indent, stripped, lineno))
    return toks


def _is_map_item(text):
    """A block-sequence item is a mapping if it looks like `key: ...` or `key:`."""
    return ": " in text or text.endswith(":")


def _parse_scalar(s, lineno):
    s = s.strip()
    if s.startswith('"'):
        buf, i = [], 1
        while i < len(s):
            c = s[i]
            if c == "\\" and i + 1 < len(s):
                buf.append(s[i + 1]); i += 2; continue
            if c == '"':
                return "".join(buf)
            buf.append(c); i += 1
        raise MiniYAMLError(f"line {lineno}: unterminated double quote")
    if s.startswith("'"):
        end = s.find("'", 1)
        if end == -1:
            raise MiniYAMLError(f"line {lineno}: unterminated single quote")
        return s[1:end]
    if s.startswith("["):
        return _parse_flow_seq(s, lineno)
    # plain scalar: strip an inline comment (no quotes to worry about here)
    h = s.find(" #")
    if h != -1:
        s = s[:h].rstrip()
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    return s


def _parse_flow_seq(s, lineno):
    end = s.find("]")
    if end == -1:
        raise MiniYAMLError(f"line {lineno}: unterminated flow sequence '{s}'")
    inner = s[1:end].strip()
    if not inner:
        return []
    return [p.strip().strip("\"'") for p in inner.split(",") if p.strip()]


def _parse_node(toks, pos, min_indent):
    if pos >= len(toks) or toks[pos][0] < min_indent:
        return None, pos
    if toks[pos][1].startswith("- "):
        return _parse_seq(toks, pos, toks[pos][0])
    return _parse_map(toks, pos, toks[pos][0])


def _read_value(rest, toks, pos, indent, lineno):
    """Resolve the value that follows `key:` — nested block, block scalar, or scalar."""
    if rest == "":
        if pos < len(toks) and toks[pos][0] > indent:
            return _parse_node(toks, pos, indent + 1)
        return None, pos
    if rest in (">", "|") or rest[0] in (">", "|"):
        buf = []
        while pos < len(toks) and toks[pos][0] > indent:
            buf.append(toks[pos][1]); pos += 1
        return " ".join(buf), pos
    return _parse_scalar(rest, lineno), pos


def _parse_map(toks, pos, indent):
    result = {}
    while pos < len(toks):
        ci, cc, ln = toks[pos]
        if ci < indent:
            break
        if ci > indent:
            raise MiniYAMLError(f"line {ln}: unexpected indent (map)")
        if cc.startswith("- "):
            raise MiniYAMLError(f"line {ln}: sequence item in mapping context")
        key, sep, rest = cc.partition(":")
        if not sep:
            raise MiniYAMLError(f"line {ln}: expected 'key: value', got '{cc}'")
        pos += 1
        val, pos = _read_value(rest.strip(), toks, pos, indent, ln)
        result[key.strip()] = val
    return result, pos


def _parse_seq(toks, pos, indent):
    result = []
    while pos < len(toks):
        ci, cc, ln = toks[pos]
        if ci < indent or not cc.startswith("- "):
            break
        if ci > indent:
            raise MiniYAMLError(f"line {ln}: unexpected indent (seq)")
        item = cc[2:].strip()
        pos += 1
        if item == "":
            val, pos = _parse_node(toks, pos, indent + 1)
        elif _is_map_item(item):
            # Block sequence of maps ("- id: X" then deeper keys). Re-fold into a
            # standalone mapping at indent+2 and reuse _parse_map.
            sub = [(indent + 2, item, ln)]
            while pos < len(toks) and toks[pos][0] > indent:
                sub.append(toks[pos]); pos += 1
            val, _ = _parse_map(sub, 0, indent + 2)
        else:
            val = _parse_scalar(item, ln)
        result.append(val)
    return result, pos


def _strip_code_fence(text):
    """Drop a surrounding ``` fence.

    The sizer's own agent file says "no code fences", and a live PLAN-tier model
    emitted one anyway on the very first real run — rocket.sh writes the agent's
    answer to <slug>.units.yml verbatim, so EVERY live-sized map failed to parse
    ("line 1: expected 'key: value', got '```yaml'") and fan-out could never
    engage. Tolerating the wrapper here is the difference between a harness that
    works with a real model and one that only works with hand-written fixtures.
    Only a fence that wraps the WHOLE document is removed; a stray fence in the
    middle still fails loud, as it should.
    """
    lines = text.splitlines()
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is None or not lines[first].lstrip().startswith("```"):
        return text
    last = next((i for i in range(len(lines) - 1, first, -1) if lines[i].strip()), None)
    if last is None or lines[last].strip() != "```":
        return text
    return "\n".join(lines[first + 1:last])


def load_yaml_subset(text):
    toks = _tokenize(_strip_code_fence(text))
    if not toks:
        return {}
    val, pos = _parse_node(toks, 0, 0)
    if pos != len(toks):
        _, _, ln = toks[pos]
        raise MiniYAMLError(f"line {ln}: could not parse (unexpected structure)")
    return val


# ─────────────────────────────────────────────────────────────────────────────
# 2. Data model
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Unit:
    id: str
    role: str
    model: str
    owns: set
    reads: set
    depends_on: list
    on_failure: str
    test: str = ""


@dataclass
class Feature:
    feature: str
    slug: str
    depends_on: list
    failure_policy: str
    shape: str
    units: list
    path: str = ""

    def combined_owns(self):
        s = set()
        for u in self.units:
            s |= u.owns
        return s


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    raise MiniYAMLError(f"expected a list, got {v!r}")


def load_feature(path):
    return load_feature_from_text(open(path, encoding="utf-8").read(), path)


def load_feature_from_text(text, path="<text>"):
    data = load_yaml_subset(text)
    if not isinstance(data, dict):
        raise MiniYAMLError(f"{path}: top level is not a mapping")
    for k in ("feature", "slug", "units"):
        if k not in data:
            raise MiniYAMLError(f"{path}: missing top-level key '{k}'")
    units = []
    for u in _as_list(data["units"]):
        if not isinstance(u, dict):
            raise MiniYAMLError(f"{path}: a units entry is not a mapping")
        for k in ("id", "role", "owns"):
            if k not in u:
                raise MiniYAMLError(f"{path}: unit missing '{k}': {u.get('id', u)}")
        units.append(
            Unit(
                id=u["id"],
                role=u["role"],
                model=u.get("model", ""),
                owns=set(_as_list(u.get("owns"))),
                reads=set(_as_list(u.get("reads"))),
                depends_on=list(_as_list(u.get("depends_on"))),
                on_failure=u.get("on_failure", ""),
                test=(u.get("test") or "").replace("\t", " "),
            )
        )
    sizing = data.get("sizing") or {}
    return Feature(
        feature=data["feature"],
        slug=data["slug"],
        depends_on=list(_as_list(data.get("depends_on"))),
        failure_policy=data.get("failure_policy", ""),
        shape=sizing.get("shape", "") if isinstance(sizing, dict) else "",
        units=units,
        path=path,
    )


def load_plan_dir(plan_dir):
    feats = {}
    for path in sorted(glob.glob(os.path.join(plan_dir, "*.units.yml"))):
        f = load_feature(path)
        feats[f.feature] = f
    return feats


# ─────────────────────────────────────────────────────────────────────────────
# 3. Validation — the two hard rules (§4.3)
# ─────────────────────────────────────────────────────────────────────────────
def validate_feature(feat):
    """Return a list of error strings; empty == valid."""
    errors = []
    # Complete ownership: no file owned by more than one unit (nothing co-owned).
    for a, b in itertools.combinations(feat.units, 2):
        shared = a.owns & b.owns
        if shared:
            errors.append(
                f"co-ownership: {a.id} & {b.id} both own {sorted(shared)}"
            )
    # Contracts are read-only downstream: a non-contract unit must not OWN a file
    # owned by a contract unit (co-ownership check above already forbids overlap,
    # but we report it with the freeze-first framing when it involves a contract).
    contract_owns = set()
    for u in feat.units:
        if u.role == "contract":
            contract_owns |= u.owns
    for u in feat.units:
        if u.role != "contract":
            bad = u.owns & contract_owns
            if bad:
                errors.append(
                    f"contract violation: downstream {u.id} owns frozen contract "
                    f"file(s) {sorted(bad)} (must be in 'reads', not 'owns')"
                )
    # Dependency integrity: every unit depends_on id must exist in the feature.
    ids = {u.id for u in feat.units}
    for u in feat.units:
        for d in u.depends_on:
            if d not in ids:
                errors.append(f"{u.id} depends_on unknown unit '{d}'")
    return errors


# ─────────────────────────────────────────────────────────────────────────────
# 4. Partitioning — within a feature and across features
# ─────────────────────────────────────────────────────────────────────────────
def _effective_deps(feat):
    """Explicit unit deps PLUS structural freeze-first edges (§5.3), so the ordering
    holds even if a map under-specifies: every non-contract unit waits for all
    contract units; every integration unit waits for all fan-out units."""
    contract_ids = [u.id for u in feat.units if u.role == "contract"]
    fanout_ids = [u.id for u in feat.units if u.role == "fanout"]
    eff = {}
    for u in feat.units:
        deps = set(u.depends_on)
        if u.role != "contract":
            deps |= set(contract_ids)
        if u.role == "integration":
            deps |= set(fanout_ids)
        deps.discard(u.id)
        eff[u.id] = deps
    return eff


def schedule_within_feature(feat):
    """Ordered list of steps; each step is a list of Units that could run
    concurrently (pairwise-disjoint owns, all deps satisfied). Sequential features
    yield one single-unit step per unit. Raises ScheduleError on a cycle."""
    eff = _effective_deps(feat)
    by_id = {u.id: u for u in feat.units}
    done, remaining, steps = set(), list(feat.units), []
    while remaining:
        ready = [u for u in remaining if eff[u.id] <= done]
        if not ready:
            stuck = ", ".join(u.id for u in remaining)
            raise ScheduleError(f"{feat.feature}: dependency cycle or missing dep among: {stuck}")
        group, owned = [], set()
        for u in ready:
            if u.owns & owned:      # collides with an earlier pick this step → defer
                continue
            group.append(u); owned |= u.owns
        steps.append(group)
        for u in group:
            done.add(u.id); remaining.remove(u)
    return steps


def _transitive_feature_deps(feats):
    """id -> set of feature ids it (transitively) depends on, within the given set."""
    closure = {}

    def walk(fid, seen):
        if fid in closure:
            return closure[fid]
        acc = set()
        for d in feats.get(fid, Feature("", "", [], "", "", [])).depends_on:
            if d in feats and d not in seen:
                acc.add(d)
                acc |= walk(d, seen | {fid})
        closure[fid] = acc
        return acc

    for fid in feats:
        walk(fid, set())
    return closure


def cross_feature_pairs(feats):
    """For every pair of features present, classify: CONCURRENT, SEQUENTIAL (dep
    edge), or SERIALIZE (file collision). Deps are only visible among the maps
    present — real run-eligibility (all queue deps SHIPPED) is enforced separately
    by get_next_feature in rocket.sh."""
    dep = _transitive_feature_deps(feats)
    out = []
    for a, b in itertools.combinations(sorted(feats), 2):
        fa, fb = feats[a], feats[b]
        edge = a in dep.get(b, set()) or b in dep.get(a, set())
        overlap = fa.combined_owns() & fb.combined_owns()
        if edge:
            verdict, detail = "SEQUENTIAL", "dependency edge"
        elif overlap:
            verdict, detail = "SERIALIZE", f"file collision: {sorted(overlap)}"
        else:
            verdict, detail = "CONCURRENT", "disjoint, no visible dep edge"
        out.append((a, b, verdict, detail))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 5. Reporting
# ─────────────────────────────────────────────────────────────────────────────
def build_report(feats, max_parallel):
    lines, ok = [], True
    lines.append("═" * 70)
    lines.append("ROCKET FAN-OUT SCHEDULER — DRY RUN (execution stays sequential)")
    lines.append(f"maps: {len(feats)}   MAX_PARALLEL: {max_parallel}")
    lines.append("═" * 70)
    for fid in sorted(feats):
        feat = feats[fid]
        errs = validate_feature(feat)
        lines.append("")
        lines.append(f"● {feat.feature}  [{feat.shape or '?'}]  failure_policy={feat.failure_policy or '?'}")
        lines.append(f"  deps: {feat.depends_on or '—'}")
        if errs:
            ok = False
            lines.append("  VALIDATION: ✗ FAIL")
            for e in errs:
                lines.append(f"    - {e}")
            continue
        lines.append("  VALIDATION: ✓ disjoint + complete ownership")
        try:
            steps = schedule_within_feature(feat)
        except ScheduleError as e:
            ok = False
            lines.append(f"  SCHEDULE: ✗ {e}")
            continue
        if len(steps) == 1 and len(steps[0]) == 1:
            lines.append("  SCHEDULE: solo (1 unit) — nothing to fan out")
        else:
            lines.append("  SCHEDULE (freeze-first → fan-out → integration):")
        for i, step in enumerate(steps, 1):
            tag = "∥ concurrent" if len(step) > 1 else "sequential"
            names = ", ".join(f"{u.id}({u.model or '?'})" for u in step)
            wave = ""
            if len(step) > max_parallel:
                wave = f"  [>{max_parallel}: drains in waves]"
            lines.append(f"    step {i} [{tag}]: {names}{wave}")
    # cross-feature
    lines.append("")
    lines.append("─" * 70)
    lines.append("CROSS-FEATURE (which eligible features may run TOGETHER):")
    lines.append("  note: dep edges only visible among present maps; run-eligibility")
    lines.append("  (all queue deps SHIPPED) is enforced separately by get_next_feature.")
    for a, b, verdict, detail in cross_feature_pairs(feats):
        mark = {"CONCURRENT": "∥", "SEQUENTIAL": "→", "SERIALIZE": "✗"}[verdict]
        lines.append(f"    {a} {mark} {b}: {verdict} ({detail})")
    lines.append("")
    lines.append("═" * 70)
    lines.append("RESULT: " + ("✓ all maps valid & schedulable" if ok else "✗ validation/schedule errors — BLOCK"))
    lines.append("Reminder: step-2 scheduler is READ-ONLY. rocket.sh still builds one")
    lines.append("feature at a time, sequentially. Fan-out execution is step 3.")
    lines.append("═" * 70)
    return "\n".join(lines), ok


def emit_plan(feats, feature_id, out=sys.stdout):
    """Print a tab-separated, bash-parseable execution plan for ONE feature, in
    step order (freeze-first → fan-out groups → integration). Consumed by
    .claude/rocket_fanout.sh so the orchestrator never re-parses YAML. Columns:

        step  unit_id  role  model  on_failure  owns_csv  reads_csv  test

    owns/reads are comma-joined (file paths have no commas); `test` is last so a
    bash `read` can slurp it whole. Returns 0 on success; fails loud (returns 1,
    prints reasons to stderr) if the feature is missing/invalid/unschedulable so
    the caller BLOCKS rather than fanning out on a bad map."""
    if feature_id not in feats:
        print(f"rocket-schedule: no map for feature '{feature_id}'", file=sys.stderr)
        return 1
    feat = feats[feature_id]
    errs = validate_feature(feat)
    if errs:
        for e in errs:
            print(f"rocket-schedule: {feature_id}: {e}", file=sys.stderr)
        return 1
    try:
        steps = schedule_within_feature(feat)
    except ScheduleError as e:
        print(f"rocket-schedule: {e}", file=sys.stderr)
        return 1
    for i, step in enumerate(steps, 1):
        for u in step:
            # Empty fields become "-" so a bash `read` (tab is IFS-whitespace and
            # collapses adjacent tabs) can't misalign the columns. Bash maps "-"
            # back to empty for owns/reads/test.
            owns = ",".join(sorted(u.owns)) or "-"
            reads = ",".join(sorted(u.reads)) or "-"
            fields = [str(i), u.id, u.role, u.model or "-", u.on_failure or "-",
                      owns, reads, u.test or "-"]
            out.write("\t".join(fields) + "\n")
    return 0


def build_json(feats, max_parallel):
    out = {"max_parallel": max_parallel, "features": {}, "cross_feature": []}
    all_ok = True
    for fid, feat in feats.items():
        errs = validate_feature(feat)
        entry = {
            "shape": feat.shape,
            "failure_policy": feat.failure_policy,
            "depends_on": feat.depends_on,
            "valid": not errs,
            "errors": errs,
            "steps": [],
        }
        if not errs:
            try:
                for step in schedule_within_feature(feat):
                    entry["steps"].append(
                        [{"id": u.id, "role": u.role, "model": u.model} for u in step]
                    )
            except ScheduleError as e:
                entry["valid"] = False
                entry["errors"].append(str(e))
        all_ok = all_ok and entry["valid"]
        out["features"][fid] = entry
    for a, b, verdict, detail in cross_feature_pairs(feats):
        out["cross_feature"].append({"a": a, "b": b, "verdict": verdict, "detail": detail})
    out["ok"] = all_ok
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 6. CLI
# ─────────────────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(description="Rocket fan-out do-no-harm scheduler (dry run).")
    ap.add_argument("--plan-dir", default="features/_plan")
    ap.add_argument("--max-parallel", type=int,
                    default=int(os.environ.get("MAX_PARALLEL", "3")))
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--emit-plan", metavar="FEATURE",
                    help="print the tab-separated execution plan for ONE feature")
    args = ap.parse_args(argv)

    # The report uses box-drawing/marks; force UTF-8 so it renders on a Windows
    # cp1252 console too (the Linux container is UTF-8 already).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    if not os.path.isdir(args.plan_dir):
        print(f"rocket-schedule: no plan dir '{args.plan_dir}'", file=sys.stderr)
        return 2
    try:
        feats = load_plan_dir(args.plan_dir)
    except MiniYAMLError as e:
        print(f"rocket-schedule: FAIL parsing ownership map: {e}", file=sys.stderr)
        return 2
    if not feats:
        print(f"rocket-schedule: no *.units.yml maps in '{args.plan_dir}'", file=sys.stderr)
        return 0

    if args.emit_plan:
        return emit_plan(feats, args.emit_plan)

    if args.json:
        payload = build_json(feats, args.max_parallel)
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    report, ok = build_report(feats, args.max_parallel)
    print(report)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
