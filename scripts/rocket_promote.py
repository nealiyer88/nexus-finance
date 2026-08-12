#!/usr/bin/env python3
"""Draft → queue promotion (the human gate, without the hand-editing).

WHY THIS EXISTS
---------------
Plan mode writes proposed features into ``features/_drafts/``. Nothing is ever
auto-queued — that gate is deliberate and stays. But *acting* on the gate used
to mean opening ``FEATURE_QUEUE.md`` and hand-editing a pipe-delimited table.

That table is not documentation, it is a parser input. ``rocket.sh`` reads it
with anchored greps and ``awk -F'|'`` field extraction:

    get_next_feature   grep -E "^\\| ${FEATURE_ID_REGEX} "  then IFS='|' read
    get_brief_path     grep -E "^\\| <id> "  | awk -F'|' '{print $4}'
    feature_deps       grep -E "^\\| <id> "  | awk -F'|' '{print $5}'
    block_in_queue     sed  "/^| <id> /{s/ QUEUED / BLOCKED /;}"
    _count_status      grep -acE "^\\| .* QUEUED "

A stray ``|`` inside a feature name shifts every field after it. A missing space
after the id makes the anchored grep miss the row entirely. Either way the row
is still *there*, still looks right to a human, and the failure surfaces hours
later as a feature that never builds. This module generates the row from that
schema instead, and then proves the readers can read what it wrote.

It writes; it never decides. Which drafts get promoted is still entirely the
human's call — ``list`` exists so that call can be made without reading a brief.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Do not leave bytecode in the user's project. Importing the sibling below makes
# the interpreter write scripts/__pycache__/ next to it, that directory is
# untracked, and rocket.sh's dirty-tree guard then REFUSES TO START — the harness
# blocked by a file the harness itself produced, one command earlier. Promote is
# the documented plan-to-queue step, so `./rocket.sh promote X` followed by
# `./rocket.sh` was a two-command path into a stuck repo.
#
# install.sh also gitignores the directory, which covers any other Python the
# harness runs; this stops it existing at all for the one import we control.
# Never observed on macOS: Apple's Command Line Tools Python sets a non-standard
# sys.pycache_prefix under ~/Library/Caches, so bytecode is never written into the
# project there. Stock CPython puts it beside the source.
sys.dont_write_bytecode = True
try:
    from rocket_schedule import load_feature_from_text, load_yaml_subset  # noqa: E402
except Exception:  # pragma: no cover - the sizer half is optional
    load_feature_from_text = None
    load_yaml_subset = None


# ── The queue schema, in one place ────────────────────────────────────────────
# Column ORDER is load-bearing (rocket.sh parses by position, not by header
# name). Under `awk -F'|'` the leading pipe makes $1 empty, so the fields are
# $2=id $3=name $4=brief $5=depends $6=status — which is why HEADER below is
# checked before anything is written. A queue whose columns were reordered by
# hand is not a queue this can safely append to.
HEADER = ("#", "Feature", "Brief", "Depends On", "Status")
NO_DEPS = "—"          # what rocket.sh's dep loop treats as "no dependencies"
NEW_STATUS = "QUEUED"

# Anything that would corrupt a cell. A pipe ends the field; CR/LF ends the row;
# a tab survives `xargs` unevenly across platforms. None of these are escapable
# in a markdown table that awk splits on '|', so they are rewritten, never
# passed through with a backslash.
CELL_POISON = re.compile(r"[|\r\n\t]")
WS_RUN = re.compile(r"\s+")

# rocket.sh trims every field it reads with `xargs` (get_next_feature,
# get_brief_path, feature_deps all do). `xargs` performs SHELL QUOTE REMOVAL,
# so a feature named  User's login  makes it die with "unmatched single quote"
# and the whole row — the whole QUEUE — stops being readable. Quotes and
# backslashes are therefore stripped from generated cells, not escaped: there
# is no escaping that survives `xargs`.
QUOTE_POISON = re.compile(r"[\"'`\\]")

MAX_NAME = 60          # keeps the table readable; the brief holds the detail
MAX_LINE = 100         # one-line summaries stay one line


class PromoteError(Exception):
    """A refusal. Always carries the sentence a non-technical user needs."""


# ── small text helpers ────────────────────────────────────────────────────────
def clean_cell(text, limit=MAX_NAME):
    """Make ANY string safe to sit in a table cell.

    Returns (cleaned, was_changed). Pipes become '/' rather than vanishing so a
    name like "import|export" still reads as "import/export"; newlines and tabs
    collapse to a space. This is the escape half of the contract — the reject
    half (ids, paths) lives in validate_id / validate_path, because a pipe in a
    file path is not a formatting problem, it is a wrong path.
    """
    original = text or ""
    out = original.replace("|", "/")
    out = CELL_POISON.sub(" ", out)
    out = QUOTE_POISON.sub("", out)
    out = WS_RUN.sub(" ", out).strip()
    if limit and len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out, (out != WS_RUN.sub(" ", original).strip())


def one_line(text, limit=MAX_LINE):
    out, _ = clean_cell(text, limit=None)
    if len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


def validate_id(fid, id_regex):
    """Ids are REJECTED, not sanitised.

    The id is interpolated into a grep -E pattern and a sed address by
    rocket.sh, so anything outside FEATURE_ID_REGEX is both un-findable and a
    regex-injection hazard. FEATURE_ID_REGEX is the project's own config value —
    matching it is exactly the promise "get_next_feature will select this row".
    """
    if not fid:
        raise PromoteError("no feature id — cannot write a row without one")
    if CELL_POISON.search(fid) or " " in fid:
        raise PromoteError(f"feature id {fid!r} contains a space or a table character")
    if not re.fullmatch(r"(?:%s)" % id_regex, fid):
        raise PromoteError(
            f"feature id {fid!r} does not match this project's FEATURE_ID_REGEX "
            f"({id_regex}) — rocket.sh would never select the row. "
            f"Fix the id in the draft, or widen FEATURE_ID_REGEX in rocket.config.sh."
        )
    return fid


def validate_path(path):
    if CELL_POISON.search(path):
        raise PromoteError(f"brief path {path!r} contains a table character")
    if path != path.strip():
        raise PromoteError(f"brief path {path!r} has leading/trailing whitespace")
    # get_brief_path pipes this cell through `xargs`, which does shell word
    # splitting and quote removal. A space or a quote in the filename means the
    # builder is handed a path that is not the file. Rename the draft instead.
    if QUOTE_POISON.search(path) or " " in path:
        raise PromoteError(
            f"brief filename {os.path.basename(path)!r} contains a space or a quote. "
            f"rocket.sh reads this cell through `xargs` and would hand the builder a "
            f"different path. Rename the draft file to use only letters, digits, "
            f"dots and dashes."
        )
    return path


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── the live queue ────────────────────────────────────────────────────────────
class Queue:
    """FEATURE_QUEUE.md as rocket.sh sees it: header, separator, rows."""

    def __init__(self, path):
        self.path = path
        self.lines = []
        self.header_idx = None
        self.last_row_idx = None
        self.rows = []          # (line index, [cells])
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            raise PromoteError(
                f"no queue file at {self.path}. Run the installer, or copy "
                f"templates/FEATURE_QUEUE.md into place, before promoting."
            )
        self.lines = open(self.path, encoding="utf-8", errors="replace").read().split("\n")
        for i, line in enumerate(self.lines):
            if not line.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            self.last_row_idx = i
            if self.header_idx is None:
                self.header_idx = i
                self.header = cells
                continue
            if set("".join(cells)) <= set("-: "):     # the |---|---| separator
                continue
            self.rows.append((i, cells))
        if self.header_idx is None:
            raise PromoteError(
                f"{self.path} has no table in it at all — nothing to append to."
            )
        want = [h.lower() for h in HEADER]
        got = [h.lower() for h in self.header[: len(HEADER)]]
        if got != want or len(self.header) < len(HEADER):
            raise PromoteError(
                f"{self.path} does not have the columns rocket.sh parses.\n"
                f"        expected: {' | '.join(HEADER)}\n"
                f"        found:    {' | '.join(self.header)}\n"
                f"        Column order is load-bearing — restore it before promoting."
            )

    def ids(self):
        return {cells[0]: cells for _, cells in self.rows}

    def row_index_of(self, fid):
        for i, cells in self.rows:
            if cells[0] == fid:
                return i
        return None

    def status_of(self, fid):
        for _, cells in self.rows:
            if cells[0] == fid:
                return cells[4] if len(cells) > 4 else "?"
        return None

    def append_row(self, cells):
        """Insert after the LAST table line.

        Appending is what makes dependency order correct: every dependency was
        verified to be in the queue already, so a row placed at the end is
        below all of them by construction. (`promote --all` orders the batch
        topologically first, so the same holds within a batch.)
        """
        row = "| " + " | ".join(cells) + " |"
        at = self.last_row_idx + 1
        self.lines.insert(at, row)
        return row

    def save(self):
        text = "\n".join(self.lines)
        tmp = self.path + ".promote.tmp.%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, self.path)


def parse_table(text):
    """Any markdown table → list of cell-lists (header and separator dropped)."""
    rows = []
    header = None
    for line in text.split("\n"):
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        rows.append(cells)
    return rows


# ── drafts ────────────────────────────────────────────────────────────────────
SKIP_DRAFT = re.compile(r"(QUEUE|DECISION)", re.I)


class Draft:
    def __init__(self, path):
        self.path = path
        self.basename = os.path.basename(path)
        self.stem = self.basename[:-3] if self.basename.endswith(".md") else self.basename
        self.text = open(path, encoding="utf-8", errors="replace").read()
        self.fid = ""
        self.name = ""
        self.deps = []
        self.dest = ""
        self.summary = ""
        self.touches = []
        self.size = ""
        self.cost = ""
        self.flags = []
        self.map_path = ""
        self.map_entry = None
        self.id_source = ""
        self.error = ""       # set when this draft cannot be promoted at all

    # brief sections ----------------------------------------------------------
    def section(self, title):
        """Body of a '## <title>' section (case-insensitive), '' if absent."""
        pat = re.compile(r"^#{1,4}\s*%s\s*$" % re.escape(title), re.I | re.M)
        m = pat.search(self.text)
        if not m:
            return ""
        rest = self.text[m.end():]
        nxt = re.search(r"^#{1,4}\s+\S", rest, re.M)
        return (rest[: nxt.start()] if nxt else rest).strip()

    def first_prose_line(self, body):
        for raw in body.split("\n"):
            line = raw.strip().lstrip("-*0123456789. ").strip()
            if not line or line.startswith("<!--") or line.startswith("#"):
                continue
            return line
        return ""


def discover_drafts(drafts_dir, queue_basename):
    """Every draft brief awaiting a decision.

    Same exclusion set as plan mode's sizer loop: the proposed queue, the
    decision log and README are not features. Rejected drafts live under
    .rejected/ and the glob does not reach them — which is what stops a
    turned-down draft from reappearing in --list forever.
    """
    if not os.path.isdir(drafts_dir):
        return []
    out = []
    for path in sorted(glob.glob(os.path.join(drafts_dir, "*.md"))):
        base = os.path.basename(path)
        if base == queue_basename or base.lower() == "readme.md" or SKIP_DRAFT.search(base):
            continue
        out.append(Draft(path))
    return out


def find_proposed_queue(drafts_dir):
    """The reconciler's FEATURE_QUEUE.proposed.md → {brief basename: row}."""
    if not os.path.isdir(drafts_dir):
        return {}
    cands = [p for p in sorted(glob.glob(os.path.join(drafts_dir, "*.md")))
             if "QUEUE" in os.path.basename(p).upper()]
    proposed = {}
    for path in cands:
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for cells in parse_table(text):
            if len(cells) < 5:
                continue
            brief = cells[2]
            if not brief:
                continue
            proposed.setdefault(os.path.basename(brief), cells)
    return proposed


def load_maps(plan_dir):
    """Index the sizer's ownership maps by slug and by lowercased feature id."""
    maps = {}
    if not os.path.isdir(plan_dir) or load_feature_from_text is None:
        return maps
    for path in sorted(glob.glob(os.path.join(plan_dir, "*.units.yml"))):
        text = open(path, encoding="utf-8", errors="replace").read()
        entry = {"path": path, "feature": None, "raw": None, "error": ""}
        try:
            entry["feature"] = load_feature_from_text(text, path)
            entry["raw"] = load_yaml_subset(text) if load_yaml_subset else None
        except Exception as exc:
            entry["error"] = str(exc)
        stem = os.path.basename(path)[: -len(".units.yml")]
        maps[stem.lower()] = entry
        feat = entry.get("feature")
        if feat is not None:
            maps.setdefault(str(feat.feature).lower(), entry)
            if feat.slug:
                maps.setdefault(str(feat.slug).lower(), entry)
    return maps


def map_is_approved(plan_dir, map_path):
    rec = os.path.join(plan_dir, ".approved",
                       os.path.basename(map_path)[: -len(".units.yml")] + ".sha256")
    if not os.path.exists(rec):
        return False
    want = ""
    for line in open(rec, encoding="utf-8", errors="replace"):
        if line.startswith("sha256:"):
            want = line.split(":", 1)[1].strip()
            break
    try:
        return bool(want) and want == sha256_file(map_path)
    except OSError:
        return False


# ── enrichment: everything --list needs to make a decision readable ───────────
def enrich(draft, proposed, maps, ctx):
    q = ctx["queue"]
    row = proposed.get(draft.basename)

    # ---- id -----------------------------------------------------------------
    fid, source = "", ""
    if row and row[0]:
        fid, source = row[0], "the proposed queue"
    entry = maps.get(draft.stem.lower())
    if not fid and entry and entry.get("feature") is not None:
        fid, source = str(entry["feature"].feature), "the ownership map"
    if not fid:
        m = re.match(r"^([A-Za-z0-9]+)-", draft.stem)
        if m:
            fid, source = m.group(1), "the draft's filename"
    draft.fid, draft.id_source = fid, source
    if entry is None and fid:
        entry = maps.get(fid.lower())
    draft.map_entry = entry
    draft.map_path = entry["path"] if entry else ""

    # ---- name ---------------------------------------------------------------
    raw_name = ""
    if row and len(row) > 1:
        raw_name = row[1]
    if not raw_name:
        m = re.search(r"^#\s*(?:Feature Brief:\s*)?(.+)$", draft.text, re.M)
        raw_name = m.group(1) if m else draft.stem
    draft.name, changed = clean_cell(raw_name)
    if changed:
        draft.flags.append("its name contained a table character — rewritten so the row parses")
    if not draft.name:
        draft.name = clean_cell(draft.stem)[0]

    # ---- dependencies -------------------------------------------------------
    deps_raw = ""
    if row and len(row) > 3:
        deps_raw = row[3]
    if deps_raw.strip() in ("", NO_DEPS, "-", "--", "none", "None"):
        deps_raw = ""
    deps = [d.strip() for d in deps_raw.split(",") if d.strip()] if deps_raw else []
    if not deps and entry and entry.get("feature") is not None:
        deps = [str(d).strip() for d in entry["feature"].depends_on if str(d).strip()]
    draft.deps = deps

    # ---- what it does -------------------------------------------------------
    body = draft.section("Proposed Solution") or draft.section("Problem Statement")
    draft.summary = one_line(draft.first_prose_line(body)) or one_line(draft.name)

    # ---- what it touches ----------------------------------------------------
    touches = []
    if entry and entry.get("feature") is not None:
        touches = sorted(entry["feature"].combined_owns())
    if not touches:
        rel = draft.section("Relevant Files")
        touches = re.findall(r"`([^`]+)`", rel)
        if not touches:
            touches = re.findall(r"`([\w./-]+\.[A-Za-z0-9]{1,5})`", draft.text)
    seen, uniq = set(), []
    for t in touches:
        t = one_line(t, limit=80)
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    draft.touches = uniq

    # ---- size / cost --------------------------------------------------------
    if entry and entry.get("feature") is not None:
        feat = entry["feature"]
        mix = {}
        for u in feat.units:
            mix[u.model or "unset"] = mix.get(u.model or "unset", 0) + 1
        mixed = ", ".join(f"{k} x{v}" for k, v in sorted(mix.items()))
        draft.size = f"{len(feat.units)} work unit(s) [{mixed}], shape {feat.shape or '?'}"
    elif entry:
        draft.size = "ownership map present but unreadable"
    else:
        draft.size = "no ownership map — will build as one agent, solo"

    cost = ""
    raw = entry.get("raw") if entry else None
    if isinstance(raw, dict):
        for key in ("estimated_cost", "estimate", "cost", "budget"):
            if key in raw and not isinstance(raw[key], (dict, list)):
                cost = str(raw[key])
                break
        sizing = raw.get("sizing")
        if not cost and isinstance(sizing, dict):
            for key in ("estimated_cost", "estimate", "cost", "budget", "size"):
                if key in sizing and not isinstance(sizing[key], (dict, list)):
                    cost = str(sizing[key])
                    break
    if not cost:
        m = re.search(r"^\s*(?:[-*]\s*)?\**\s*(?:estimated|est\.?)\s*"
                      r"(?:cost|size|effort|spend)\**\s*[:=]\s*(.+)$",
                      draft.text, re.I | re.M)
        if m:
            cost = m.group(1)
    draft.cost = one_line(cost, limit=40) if cost else ""

    # ---- flags a human should see BEFORE saying yes --------------------------
    if entry and entry.get("error"):
        draft.flags.append("its ownership map does not parse — fan-out is not usable: "
                           + one_line(entry["error"], limit=80))
    elif entry and not map_is_approved(ctx["plan_dir"], entry["path"]):
        slug = os.path.basename(entry["path"])[: -len(".units.yml")]
        draft.flags.append(f"its parallel-build plan is not approved yet "
                           f"(run: ./rocket.sh approve {slug})")

    risks = draft.section("Risks")
    risk_line = draft.first_prose_line(risks)
    if risk_line:
        draft.flags.append("risk noted by the planners: " + one_line(risk_line, limit=90))
    if re.search(r"high[ -]risk|HIGH RISK", draft.text):
        draft.flags.append("the planners marked part of this HIGH RISK")

    for cand in (draft.stem, draft.fid.lower() if draft.fid else ""):
        if not cand:
            continue
        rc = os.path.join(ctx["reality_dir"], f"{cand}-reality-check.md")
        if os.path.exists(rc):
            text = open(rc, encoding="utf-8", errors="replace").read()
            hits = re.findall(r"VERDICT:\s*(GO|FLAG)", text)
            if hits and hits[-1] == "FLAG":
                draft.flags.append(f"a reality check FLAGGED this — see {rc}")
            break

    # ---- eligibility --------------------------------------------------------
    try:
        validate_id(draft.fid, ctx["id_regex"])
    except PromoteError as exc:
        draft.error = str(exc)
        return draft

    dest = os.path.join(ctx["features_dir"], draft.basename)
    try:
        draft.dest = validate_path(dest)
    except PromoteError as exc:
        draft.error = str(exc)
        return draft

    for dep in draft.deps:
        try:
            validate_id(dep, ctx["id_regex"])
        except PromoteError as exc:
            draft.error = f"dependency {dep!r} is unusable: {exc}"
            return draft

    existing = q.status_of(draft.fid)
    if existing is not None:
        draft.error = (
            f"id {draft.fid} already has a row in {q.path} (status {existing}). "
            f"Refusing to add a second row with the same id — that row would be "
            f"invisible to rocket.sh. Give this draft a different id, or reject it."
        )
    return draft


# ── printing ──────────────────────────────────────────────────────────────────
def print_list(drafts, ctx, out=sys.stdout):
    w = out.write
    if not drafts:
        drafts_dir = ctx["drafts_dir"]
        if not os.path.isdir(drafts_dir):
            w(f"No drafts folder yet ({drafts_dir}).\n"
              f"Drafts appear there after you run:  ./rocket.sh plan <your-plan-file>\n")
        else:
            rejected = glob.glob(os.path.join(drafts_dir, ".rejected", "*.md"))
            w(f"No drafts waiting in {drafts_dir}.\n")
            if rejected:
                w(f"({len(rejected)} previously turned down — they are kept in "
                  f"{os.path.join(drafts_dir, '.rejected')} if you change your mind.)\n")
            w("Write a plan and run:  ./rocket.sh plan <your-plan-file>\n")
        return

    w(f"Proposed features waiting for your decision — {ctx['drafts_dir']}\n")
    w("Nothing here is built, queued, or paid for until you say so.\n\n")
    for n, d in enumerate(drafts, 1):
        w(f"  [{n}]  {d.name}\n")
        w(f"        id            {d.fid or '(none — see the problem below)'}"
          + (f"   (from {d.id_source})" if d.id_source else "") + "\n")
        w(f"        what it does  {d.summary or '(the draft says nothing under Proposed Solution)'}\n")
        if d.touches:
            shown = ", ".join(d.touches[:6])
            more = f"  (+{len(d.touches) - 6} more)" if len(d.touches) > 6 else ""
            w(f"        touches       {shown}{more}\n")
        else:
            w("        touches       (the draft does not say which files)\n")
        w(f"        depends on    {', '.join(d.deps) if d.deps else 'nothing — can build first'}\n")
        w(f"        size          {d.size}\n")
        if d.cost:
            w(f"        est. cost     {d.cost}\n")
        for i, f in enumerate(d.flags):
            w(f"        {'flags' if i == 0 else '     '}         {f}\n")
        if d.error:
            w(f"        NOT PROMOTABLE  {d.error}\n")
        w(f"        draft file    {d.path}\n\n")

    ok = [d for d in drafts if not d.error]
    w(f"{len(drafts)} waiting")
    if len(ok) != len(drafts):
        w(f" ({len(drafts) - len(ok)} cannot be promoted as written — see above)")
    w(".\n\n")
    first = (ok[0].fid if ok else "<id>")
    w(f"  Accept one       ./rocket.sh promote {first}\n")
    w("  Accept them all  ./rocket.sh promote --all\n")
    w(f"  Turn one down    ./rocket.sh promote --reject {first}\n")


# ── the two mutations ─────────────────────────────────────────────────────────
def promote_one(draft, ctx, results, out=sys.stdout):
    """Move the brief into features/ and append one correct row. Returns 0/1."""
    w = out.write
    q = ctx["queue"]

    if draft.error:
        w(f"REFUSED   {draft.basename}\n          {draft.error}\n")
        return 1

    # Re-checked against the CURRENT queue, not the one enrich() saw: inside a
    # `--all` batch an earlier promotion may have just taken this id.
    existing = q.status_of(draft.fid)
    if existing is not None:
        w(f"ALREADY THERE  {draft.fid}  ·  {draft.name}\n"
          f"               {q.path} already has a row for {draft.fid} (status {existing}).\n"
          f"               Nothing changed — promoting twice is a no-op, not a duplicate.\n")
        return 1

    dest = draft.dest
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    if os.path.exists(dest):
        # Never silently overwrite a brief. Identical content is the idempotent
        # case (a half-finished earlier promote); different content is a real
        # collision and the user has to resolve it.
        if sha256_file(dest) != hashlib.sha256(draft.text.encode("utf-8")).hexdigest():
            w(f"REFUSED   {draft.fid}  ·  {draft.basename}\n"
              f"          {dest} already exists and says something different.\n"
              f"          Refusing to overwrite it. Rename the draft or delete the old brief.\n")
            return 1
        w(f"          (brief already at {dest} — reusing it)\n")
    else:
        shutil.move(draft.path, dest)

    deps_cell = ", ".join(draft.deps) if draft.deps else NO_DEPS
    cells = [draft.fid, draft.name, dest, deps_cell, NEW_STATUS]
    for c in cells:
        if CELL_POISON.search(c):
            raise PromoteError(f"internal: cell {c!r} still contains a table character")
    q.append_row(cells)
    q.save()
    q_check = Queue(q.path)

    # Structural round-trip: re-read what we just wrote, with the same splitting
    # rocket.sh uses, and check every field survived. (rocket.sh then re-checks
    # this with its OWN reader functions — see _promote_verify_row in rocket.sh.)
    got = [c for _, c in q_check.rows if c[0] == draft.fid]
    if len(got) != 1:
        raise PromoteError(f"wrote a row for {draft.fid} and read back {len(got)} — aborting")
    if got[0][:5] != cells:
        raise PromoteError(f"row for {draft.fid} does not read back as written:\n"
                           f"        wrote {cells}\n        read  {got[0][:5]}")
    new_idx = q_check.row_index_of(draft.fid)
    for dep in draft.deps:
        dep_idx = q_check.row_index_of(dep)
        if dep_idx is None:
            raise PromoteError(f"{draft.fid} depends on {dep}, which is not in the queue")
        if dep_idx > new_idx:
            raise PromoteError(f"{draft.fid} landed above its dependency {dep}")

    ctx["queue"] = q_check
    results.append(("PROMOTED", draft.fid, draft.name, dest, deps_cell))
    w(f"QUEUED    {draft.fid}  ·  {draft.name}\n"
      f"          brief       {dest}\n"
      f"          depends on  {', '.join(draft.deps) if draft.deps else 'nothing'}\n"
      f"          status      waiting for ./rocket.sh to pick it up\n")
    return 0


def reject_one(draft, ctx, results, out=sys.stdout):
    """Dismiss a draft: files move to .rejected/, nothing is deleted."""
    w = out.write
    rej = os.path.join(ctx["drafts_dir"], ".rejected")
    os.makedirs(rej, exist_ok=True)
    dest = os.path.join(rej, draft.basename)
    if os.path.exists(dest):
        dest = os.path.join(rej, f"{draft.stem}.{int(time.time())}.md")
    shutil.move(draft.path, dest)
    moved = [dest]

    if draft.map_path and os.path.exists(draft.map_path):
        mdest = os.path.join(rej, os.path.basename(draft.map_path))
        if not os.path.exists(mdest):
            shutil.move(draft.map_path, mdest)
            moved.append(mdest)

    rec = os.path.join(rej, draft.stem + ".rejected")
    with open(rec, "w", encoding="utf-8") as fh:
        fh.write("# This draft feature was turned down by a human. Nothing was deleted:\n"
                 "# the files below are kept here so the decision can be reversed by\n"
                 "# moving them back up into features/_drafts/.\n")
        fh.write(f"draft: {draft.basename}\n")
        fh.write(f"proposed_id: {draft.fid}\n")
        fh.write(f"rejected_by: {ctx['approver']}\n")
        fh.write(f"rejected_at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
        if ctx.get("reason"):
            fh.write(f"reason: {one_line(ctx['reason'], limit=200)}\n")
        for m in moved:
            fh.write(f"kept: {m}\n")
    results.append(("REJECTED", draft.fid, draft.name, dest, ""))
    w(f"TURNED DOWN  {draft.fid or draft.stem}  ·  {draft.name}\n"
      f"             kept at  {dest}\n"
      f"             It will not show up again. To undo, move that file back into "
      f"{ctx['drafts_dir']}.\n")
    return 0


# ── selection + ordering ──────────────────────────────────────────────────────
def select(drafts, targets):
    """Match user input against id, filename or stem — forgivingly."""
    chosen, missing = [], []
    for t in targets:
        hit = None
        for d in drafts:
            if t == d.fid or t == d.basename or t == d.stem:
                hit = d
                break
        if hit is None:
            for d in drafts:
                if t.lower() in (str(d.fid).lower(), d.stem.lower()):
                    hit = d
                    break
        if hit is None:
            missing.append(t)
        elif hit not in chosen:
            chosen.append(hit)
    return chosen, missing


def order_for_promotion(drafts, queue):
    """Topological order within the batch; anything whose dependency is neither
    queued nor in the batch is REFUSED, not silently queued unbuildable.

    A row whose Depends On names an id that is not in the queue can never
    satisfy get_next_feature's `grep ... SHIPPED` check — the feature would sit
    QUEUED forever, which is precisely the late, silent failure this command
    exists to prevent.
    """
    known = set(queue.ids())
    batch = {d.fid: d for d in drafts if d.fid}
    ordered, blocked = [], []
    pending = list(drafts)
    while pending:
        progressed = False
        for d in list(pending):
            unmet = [dep for dep in d.deps
                     if dep not in known and dep in batch and batch[dep] in pending]
            if unmet:
                continue
            ordered.append(d)
            pending.remove(d)
            if d.fid:
                known.add(d.fid)
            progressed = True
        if not progressed:
            blocked.extend(pending)
            break
    for d in ordered:
        for dep in d.deps:
            if dep not in known:
                d.error = d.error or (
                    f"depends on {dep}, which is not in the queue and not among the "
                    f"drafts being promoted. Promote {dep} first, or fix the draft."
                )
    for d in blocked:
        d.error = d.error or "its dependencies form a cycle with another draft — fix the drafts"
    return ordered + blocked


# ── main ──────────────────────────────────────────────────────────────────────
def build_context(args):
    return {
        "queue": Queue(args.queue),
        "drafts_dir": args.drafts_dir,
        "plan_dir": args.plan_dir,
        "features_dir": args.features_dir,
        "reality_dir": args.reality_dir,
        "id_regex": args.id_regex,
        "approver": args.approver,
        "reason": args.reason,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rocket_promote", add_help=True)
    ap.add_argument("command", choices=("list", "promote", "reject"))
    ap.add_argument("targets", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--queue", default="FEATURE_QUEUE.md")
    ap.add_argument("--drafts-dir", default="features/_drafts")
    ap.add_argument("--plan-dir", default="features/_plan")
    ap.add_argument("--features-dir", default="features")
    ap.add_argument("--reality-dir", default="features/_reality")
    ap.add_argument("--id-regex", default="[A-Za-z0-9]+")
    ap.add_argument("--approver", default="unknown")
    ap.add_argument("--reason", default="")
    ap.add_argument("--result-file", default="")
    args = ap.parse_args(argv)

    try:
        ctx = build_context(args)
    except PromoteError as exc:
        print(f"rocket: {exc}", file=sys.stderr)
        return 1

    proposed = find_proposed_queue(args.drafts_dir)
    maps = load_maps(args.plan_dir)
    drafts = [enrich(d, proposed, maps, ctx)
              for d in discover_drafts(args.drafts_dir, os.path.basename(args.queue))]

    if args.command == "list":
        print_list(drafts, ctx)
        return 0

    if args.all:
        if not drafts:
            # Empty or missing drafts folder: say what is going on and where
            # drafts come from. Exit 0 — "there was nothing to do" is a true
            # answer to --all, not a failure.
            print_list(drafts, ctx)
            return 0
        chosen, missing = list(drafts), []
    else:
        if not args.targets:
            print("rocket: which draft? Run  ./rocket.sh promote --list  to see them.",
                  file=sys.stderr)
            return 1
        chosen, missing = select(drafts, args.targets)

    # A target that matches no draft is not automatically an error. The common
    # case is the SECOND `promote 3` — the draft is gone because the first one
    # moved it into features/ and wrote the row. That is a no-op, and saying so
    # plainly is the whole point; an "unknown draft" error there would read like
    # something broke.
    unresolved = []
    already = ctx["queue"].ids()
    rej_dir = os.path.join(args.drafts_dir, ".rejected")
    for t in missing:
        if args.command == "promote" and t in already:
            cells = already[t]
            print(f"ALREADY QUEUED  {t}  ·  {cells[1]}\n"
                  f"                brief {cells[2]}, status {cells[4] if len(cells) > 4 else '?'}. "
                  f"Nothing to do.")
            continue
        if os.path.isdir(rej_dir) and (
                os.path.exists(os.path.join(rej_dir, f"{t}.rejected"))
                or glob.glob(os.path.join(rej_dir, f"{t}*"))
                or any(t in open(p, encoding="utf-8", errors="replace").read()
                       for p in glob.glob(os.path.join(rej_dir, "*.rejected")))):
            print(f"ALREADY TURNED DOWN  {t} — its files are in {rej_dir}. Nothing to do.")
            continue
        unresolved.append(t)
        print(f"rocket: no draft matches {t!r}. Run  ./rocket.sh promote --list",
              file=sys.stderr)

    if not chosen and not unresolved:
        return 0                       # every target was an explicit no-op
    if not drafts:
        print_list(drafts, ctx)
        return 1        # NOT-RUN IS NEVER PASS: asked to promote, promoted nothing.

    results, rc = [], (1 if unresolved else 0)

    if args.command == "reject":
        for d in chosen:
            rc |= reject_one(d, ctx, results)
    else:
        for d in order_for_promotion(chosen, ctx["queue"]):
            try:
                rc |= promote_one(d, ctx, results)
            except PromoteError as exc:
                print(f"rocket: {exc}", file=sys.stderr)
                rc = 1

    if args.result_file:
        with open(args.result_file, "w", encoding="utf-8") as fh:
            for r in results:
                fh.write("\t".join(r) + "\n")
    if not results:
        rc = rc or 1
    return 1 if rc else 0


if __name__ == "__main__":
    sys.exit(main())
