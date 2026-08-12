#!/usr/bin/env python3
"""Cost dashboard — what the harness actually spent, from the agent ledger.

Every agent call already writes an index line to features/AGENT_OUTPUTS.jsonl.
Nothing read it. "~$12 spent" scrolling past in a log answers none of the
questions you have the morning after an unattended run: which feature was
expensive, which PHASE is expensive across all features, and how much went to
the expensive tier for work that did not ship.

That last one is the point. A feature that blocked still cost money, and it is
invisible in a per-feature total that mixes it with the ones that shipped — so
this splits spend by queue outcome. Cost on shipped features is the price of
the product; cost on blocked ones is the price of the harness being wrong, and
it is the number to drive down.

Stdlib only, like scripts/rocket_schedule.py — the harness must run anywhere
python3 does, with nothing installed.

Usage:
    python3 scripts/rocket_cost.py                 # whole ledger
    python3 scripts/rocket_cost.py --feature 007   # one feature, per call
    python3 scripts/rocket_cost.py --by phase      # phase | tier | feature | agent
    python3 scripts/rocket_cost.py --since 2026-07-01
    python3 scripts/rocket_cost.py --json          # machine-readable
"""

import argparse
import json
import os
import sys
from collections import defaultdict

LEDGER = "features/AGENT_OUTPUTS.jsonl"
QUEUE = "FEATURE_QUEUE.md"


def load(path, since=None):
    """Read the ledger. A malformed line is skipped and COUNTED, never fatal:
    the ledger is bookkeeping written by concurrent fan-out builders, and a
    dashboard that dies on one bad line is a dashboard nobody can use on the
    exact run they most need to inspect. The skip count is reported, because
    silently dropping records would understate spend."""
    rows, skipped = [], 0
    if not os.path.exists(path):
        return rows, skipped
    with open(path, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                skipped += 1
                continue
            if not isinstance(rec, dict):
                skipped += 1
                continue
            if since and str(rec.get("ts", "")) < since:
                continue
            rows.append(rec)
    return rows, skipped


def cost_of(rec):
    """cost_usd is null whenever the adapter could not report one — a failed
    call, a non-reporting agent. Treat it as 0 for arithmetic but count it
    separately, so a total is never quietly presented as complete when part of
    the run reported nothing."""
    raw = rec.get("cost_usd")
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def queue_status(path):
    """feature id -> SHIPPED / BLOCKED / QUEUED, straight from the queue table."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, errors="replace") as fh:
        for line in fh:
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 2:
                continue
            fid = cells[0]
            for status in ("SHIPPED", "BLOCKED", "QUEUED"):
                if status in cells:
                    out[fid] = status
                    out[fid.lower()] = status
                    break
    return out


def money(x):
    return "${:>8.2f}".format(x)


def bar(frac, width=24):
    filled = int(round(frac * width))
    return "█" * filled + "·" * (width - filled)


def group(rows, key):
    totals, calls, unpriced = defaultdict(float), defaultdict(int), defaultdict(int)
    for r in rows:
        k = str(r.get(key) or "—")
        calls[k] += 1
        c = cost_of(r)
        if c is None:
            unpriced[k] += 1
        else:
            totals[k] += c
    return totals, calls, unpriced


def print_table(title, totals, calls, unpriced, grand):
    if not totals and not calls:
        return
    print("\n" + title)
    print("─" * 72)
    order = sorted(calls, key=lambda k: (-totals.get(k, 0.0), k))
    for k in order:
        t = totals.get(k, 0.0)
        frac = (t / grand) if grand else 0.0
        note = "  ({} unpriced)".format(unpriced[k]) if unpriced[k] else ""
        print("  {:<22} {} {:>5.1f}%  {:>4} calls {}  {}".format(
            k[:22], money(t), frac * 100, calls[k], note, bar(frac)))


def main():
    ap = argparse.ArgumentParser(description="Rocket Loop cost dashboard")
    ap.add_argument("--ledger", default=LEDGER)
    ap.add_argument("--queue", default=QUEUE)
    ap.add_argument("--feature", help="drill into one feature id/slug")
    ap.add_argument("--by", choices=["feature", "phase", "tier", "agent"],
                    help="show only one breakdown")
    ap.add_argument("--since", help="ISO timestamp prefix, e.g. 2026-07-01")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    rows, skipped = load(args.ledger, args.since)
    if not rows:
        where = args.ledger
        print("No agent-call records in {}.".format(where))
        print("The ledger is written only when AGENT_LOG=1 in rocket.config.sh.")
        return 0

    if args.feature:
        want = args.feature.lower()
        rows = [r for r in rows
                if str(r.get("feature", "")).lower() in (want, want.lstrip("0"))]
        if not rows:
            print("No records for feature '{}'.".format(args.feature))
            return 1

    priced = [c for c in (cost_of(r) for r in rows) if c is not None]
    grand = sum(priced)
    n_unpriced = len(rows) - len(priced)

    if args.as_json:
        out = {"total_usd": round(grand, 4), "calls": len(rows),
               "unpriced_calls": n_unpriced, "malformed_lines": skipped}
        for key in ("feature", "phase", "tier", "agent"):
            totals, _, _ = group(rows, key)
            out[key] = {k: round(v, 4) for k, v in totals.items()}
        json.dump(out, sys.stdout, indent=2, sort_keys=True)
        print()
        return 0

    print("═" * 72)
    print("ROCKET COST — {} calls, ${:.2f}".format(len(rows), grand))
    if n_unpriced:
        # Stated up front, not in a footnote: an understated total that looks
        # authoritative is worse than one that admits what it is missing.
        print("  ⚠ {} of {} calls reported no cost — the total above is a FLOOR, "
              "not the full spend.".format(n_unpriced, len(rows)))
    if skipped:
        print("  ⚠ {} malformed ledger line(s) skipped (concurrent-append "
              "corruption?) — spend is understated by whatever they held."
              .format(skipped))
    print("═" * 72)

    if args.feature:
        print("\nCalls for {} (chronological)".format(args.feature))
        print("─" * 72)
        for r in rows:
            c = cost_of(r)
            print("  {:<20} {:<16} {:<8} {}".format(
                str(r.get("ts", ""))[:19],
                str(r.get("phase", "—"))[:16],
                str(r.get("tier", "—"))[:8],
                money(c) if c is not None else "       —"))
        return 0

    wanted = [args.by] if args.by else ["feature", "phase", "tier", "agent"]
    titles = {"feature": "By feature", "phase": "By phase",
              "tier": "By model tier", "agent": "By agent/adapter"}
    for key in wanted:
        totals, calls, unpriced = group(rows, key)
        print_table(titles[key], totals, calls, unpriced, grand)

    # Shipped vs not. The reason this file exists.
    status = queue_status(args.queue)
    if status and not args.by:
        buckets = defaultdict(float)
        for r in rows:
            fid = str(r.get("feature", ""))
            st = status.get(fid) or status.get(fid.lower()) or "UNKNOWN"
            c = cost_of(r)
            if c is not None:
                buckets[st] += c
        print("\nBy queue outcome")
        print("─" * 72)
        for st in ("SHIPPED", "BLOCKED", "QUEUED", "UNKNOWN"):
            if st not in buckets:
                continue
            frac = buckets[st] / grand if grand else 0
            print("  {:<22} {} {:>5.1f}%  {}".format(
                st, money(buckets[st]), frac * 100, bar(frac)))
        wasted = buckets.get("BLOCKED", 0.0)
        if wasted and grand:
            print("\n  ${:.2f} ({:.0f}%) went to features that did NOT ship. That is "
                  "the harness being wrong,\n  not the product being expensive — it is "
                  "the number to drive down."
                  .format(wasted, 100 * wasted / grand))
    return 0


if __name__ == "__main__":
    sys.exit(main())
