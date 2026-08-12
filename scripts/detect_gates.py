#!/usr/bin/env python3
"""Work out this project's quality gates and configure them, so nobody has to
know what a type checker is in order to have one.

WHY THIS EXISTS
---------------
gates.sh is driven by command strings in rocket.config.sh, and an empty string
means "skip this gate". That mechanism is right — it is what lets an existing
project adopt gates without every feature suddenly failing checks it never had
— but as a DEFAULT it is useless. It hands someone seven blank settings and no
way to know what belongs in them. "Configure your linter" is not an instruction,
it is a prerequisite disguised as one.

So this looks at the project, decides what it is, checks what is actually
installed, and writes the settings itself.

THE THREE THINGS IT WILL NOT DO
-------------------------------
1. **Configure a tool that is not installed.** A command that cannot run is a
   FAIL under the harness's fail-closed rule, so guessing "ruff check ." into
   the config would block every feature on a project that has no ruff. Missing
   tools are reported with the exact one-line command to install them, and the
   gate stays off until they exist.

2. **Turn on a gate your existing code already fails.** This is the difference
   between a harness you can adopt and one you cannot. Switch on a linter for a
   codebase written before you had one and the first run blocks 100% of features
   on inherited problems that have nothing to do with the feature being built.
   So every candidate gate is RUN against the current code first. Clean → it is
   enabled and blocking. Dirty → it is left off and the failure count is written
   to a debt file, because that is a backlog, not a gate.

3. **Silently decide.** Everything it concluded, and everything it declined to
   turn on and why, is printed. A tool that quietly configures things is a tool
   you cannot debug.

Usage:
    python3 scripts/detect_gates.py                      # report only, change nothing
    python3 scripts/detect_gates.py --write              # patch rocket.config.sh
    python3 scripts/detect_gates.py --install --write    # install tools first, then do both
    python3 scripts/detect_gates.py --no-probe           # skip the "does it pass?" run
    python3 scripts/detect_gates.py --root DIR           # operate on DIR, not cwd
    python3 scripts/detect_gates.py --json               # machine-readable report

`setup.sh` runs the `--install --write` form for you; this is the same thing by
hand, and the only form worth typing directly is the bare one, to see what it
would decide before letting it decide.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

PROBE_TIMEOUT = 120
_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


# ── what kind of project is this ─────────────────────────────────────────────
# Marker files, most specific first. A repo can legitimately be more than one
# (a Python API with a JS front end), so every match is kept.
ECOSYSTEMS = [
    ("python", ["pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile"]),
    ("node",   ["package.json"]),
    ("go",     ["go.mod"]),
    ("rust",   ["Cargo.toml"]),
]

# Per ecosystem, per gate: candidate tools in preference order.
#   (binary, command, install_hint)
# Preference order is "most likely already there" then "best". The point is to
# turn a gate ON, not to win an argument about tooling.
CANDIDATES = {
    "python": {
        "format":    [("ruff", "{r}ruff format .", "pip install ruff"),
                      ("black", "{r}black .", "pip install black")],
        "lint":      [("ruff", "{r}ruff check .", "pip install ruff"),
                      ("flake8", "{r}flake8 .", "pip install flake8"),
                      ("pylint", "{r}pylint .", "pip install pylint")],
        "typecheck": [("mypy", "{r}mypy .", "pip install mypy"),
                      ("pyright", "{r}pyright", "pip install pyright")],
        "coverage":  [("pytest", "{r}pytest --cov=. --cov-report=term", "pip install pytest-cov")],
        "secrets":   [("gitleaks", "gitleaks detect --no-banner", "brew install gitleaks"),
                      ("trufflehog", "trufflehog filesystem . --no-update", "brew install trufflehog")],
        "depaudit":  [("pip-audit", "{r}pip-audit", "pip install pip-audit")],
    },
    "node": {
        "format":    [("prettier", "npx --no-install prettier -w .", "npm i -D prettier")],
        "lint":      [("eslint", "npx --no-install eslint .", "npm i -D eslint")],
        "typecheck": [("tsc", "npx --no-install tsc --noEmit", "npm i -D typescript")],
        "coverage":  [("jest", "npx --no-install jest --coverage", "npm i -D jest")],
        "secrets":   [("gitleaks", "gitleaks detect --no-banner", "brew install gitleaks")],
        "depaudit":  [("npm", "npm audit --audit-level=high", "(bundled with npm)")],
    },
    "go": {
        "format":    [("gofmt", "gofmt -w .", "(bundled with go)")],
        "lint":      [("golangci-lint", "golangci-lint run", "brew install golangci-lint"),
                      ("go", "go vet ./...", "(bundled with go)")],
        "typecheck": [("go", "go build ./...", "(bundled with go)")],
        "coverage":  [("go", "go test ./... -cover", "(bundled with go)")],
        "secrets":   [("gitleaks", "gitleaks detect --no-banner", "brew install gitleaks")],
        "depaudit":  [("govulncheck", "govulncheck ./...",
                       "go install golang.org/x/vuln/cmd/govulncheck@latest")],
    },
    "rust": {
        "format":    [("cargo", "cargo fmt", "(bundled with cargo)")],
        "lint":      [("cargo", "cargo clippy -- -D warnings", "rustup component add clippy")],
        "typecheck": [("cargo", "cargo check", "(bundled with cargo)")],
        "coverage":  [("cargo-tarpaulin", "cargo tarpaulin", "cargo install cargo-tarpaulin")],
        "secrets":   [("gitleaks", "gitleaks detect --no-banner", "brew install gitleaks")],
        "depaudit":  [("cargo-audit", "cargo audit", "cargo install cargo-audit")],
    },
}

# Non-mutating equivalents, used ONLY for probing. The command we CONFIGURE is
# the rewriting one — auto-fixing formatting during a build is the point of that
# gate — but detection must be able to ask "would this pass?" without answering
# it by editing every file in the repo.
FORMAT_PROBE = {
    "ruff":     "{r}ruff format --check .",
    "black":    "{r}black --check .",
    "prettier": "npx --no-install prettier --check .",
    # gofmt -l LISTS unformatted files and still exits 0, so the exit code alone
    # says nothing; emptiness of the output is the actual signal.
    "gofmt":    "test -z \"$(gofmt -l .)\"",
    "cargo":    "cargo fmt --check",
}

# The harness installs its own files INTO the project. Linting them reports the
# harness's style as the project's debt — a novice runs detection on a codebase
# they wrote one file of and is told four files need reformatting, none of them
# theirs. Worse, it is debt they cannot fix: the next `install.sh --upgrade`
# overwrites those files anyway. So harness-owned paths are excluded from the
# gate commands we generate.
HARNESS_PATHS = [".claude", "features", "scripts/rocket_schedule.py",
                 "scripts/rocket_cost.py", "scripts/detect_gates.py"]

# How each tool spells "ignore these". Only tools with a simple flag are handled;
# for anything else the gate is generated unexcluded, which is noisier but never
# wrong. Guessing at a config-file format would be.
def exclusions_for(binary):
    # EXTEND, never replace. `--exclude` on ruff and black REPLACES the built-in
    # default ignore list rather than adding to it, so passing it silently
    # un-ignores .venv, node_modules, build dirs and the rest — the first run
    # after that reported 1607 files needing reformatting, essentially all of
    # them third-party code inside the virtualenv. The additive flag is the only
    # correct one here.
    if binary == "ruff":
        return " --extend-exclude " + ",".join(HARNESS_PATHS)
    if binary == "black":
        return " --extend-exclude '({})'".format(
            "|".join(p.replace(".", r"\.") for p in HARNESS_PATHS))
    if binary == "mypy":
        # mypy's --exclude is already additive and repeatable.
        return " " + " ".join("--exclude '{}'".format(p.replace(".", r"\."))
                              for p in HARNESS_PATHS)
    return ""


CONFIG_KEY = {"format": "FORMAT_CMD", "lint": "LINT_CMD", "typecheck": "TYPECHECK_CMD",
              "coverage": "COVERAGE_CMD", "secrets": "SECRETS_CMD", "depaudit": "DEPAUDIT_CMD",
              "test": "TEST_CMD"}

# The test gate is not optional the way the others are — it is the one check the
# whole harness is built around, and it is threaded into every agent prompt. It
# is detected here for the same reason as the rest: the alternative was a Python
# command shipped as a default, which is a guess about the language that fails
# closed and loudly in any project that is not Python.
TEST_CANDIDATES = {
    "python": [("pytest", "{r}pytest tests/ -x --tb=short", "pip install pytest")],
    "node":   [("npx", "npm test --silent", "(bundled with npm)")],
    "go":     [("go", "go test ./...", "(bundled with go)")],
    "rust":   [("cargo", "cargo test", "(bundled with cargo)")],
}


def detect_ecosystems(root):
    found = []
    for name, markers in ECOSYSTEMS:
        if any(os.path.exists(os.path.join(root, m)) for m in markers):
            found.append(name)
    if not found:
        # A project can be unmistakably Python and still have none of the marker
        # files — a plain repo with a tests/ directory and .py files is the
        # common case, and reporting "nothing to configure" for it is just wrong.
        for d in (".", "tests", "src"):
            p = os.path.join(root, d)
            if os.path.isdir(p) and any(f.endswith(".py") for f in os.listdir(p)):
                found.append("python")
                break
    return found


def venv_prefix(root):
    """Return the runner prefix for this project's virtualenv, if it has one.

    This is not cosmetic. A project whose tools live only in .venv/bin gets
    "command not found" from a bare `pytest`, and that has already caused a real
    false BLOCK in this harness's history — a feature was failed for a missing
    interpreter, not a bug.

    It is a CANDIDATE prefix, not a verdict: whether it ends up in a given
    command depends on whether that particular tool is actually in there. See
    tool_prefix(), and the bug that distinction fixed.
    """
    for d in (".venv", "venv", "env"):
        b = os.path.join(root, d, "bin")
        if os.path.isdir(b):
            return "{}/".format(os.path.join(d, "bin"))
    return ""


# Installers that only touch THIS PROJECT — a virtualenv, a node_modules — as
# opposed to the machine. The distinction is the consent boundary: --install may
# run the first kind unattended, because the blast radius is a directory you can
# delete. It will never run the second kind on its own; changing what is on
# someone's computer is their decision, not a side effect of configuring a build.
# Every hint that is genuinely a command someone could paste. A hint that is not
# one of these — "(bundled with go)", or a diagnostic we put in the missing list —
# must never be turned into a suggested command.
INSTALLERS = ("pip install", "npm i -D", "npm install", "brew install",
              "go install", "cargo install", "rustup component add")

PROJECT_SCOPED = ("pip install", "npm i -D", "npm install")


def install_cmd_for(root, hint, prefix):
    """Turn an install hint into a command scoped to this project, or None if it
    would modify the machine."""
    if not any(hint.startswith(p) for p in PROJECT_SCOPED):
        return None
    if hint.startswith("pip install"):
        pkgs = hint[len("pip install"):].strip()
        # Into the project's OWN venv when there is one. A bare `pip install`
        # would put the tool somewhere the configured command cannot see it, and
        # the gate would be written pointing at .venv/bin/<tool> that does not
        # exist — a configured gate that cannot run, i.e. a guaranteed FAIL.
        if prefix:
            return "{}pip install --quiet {}".format(prefix, pkgs)
        return "{} -m pip install --quiet {}".format(sys.executable, pkgs)
    return hint


def tool_prefix(root, binary, prefix):
    """WHERE this tool actually is — which is not the same question as whether it
    exists, and conflating the two produced a gate that could never run.

    Returns the prefix to build the command with: `prefix` when the tool is in
    the project's virtualenv, "" when it is only on PATH, None when it is
    nowhere.

    THE BUG THIS FIXES. The old version answered "does it exist anywhere?" and
    the caller then built the command with the venv prefix regardless. So a
    project that had a .venv but whose linter was installed globally — a system
    package, pipx, or just a CI runner image, i.e. the normal case on Linux and
    the overwhelmingly normal case in CI — got a gate spelled
    `.venv/bin/ruff check .` pointing at a file that does not exist. The probe
    then failed with 127 and the gate was reported as "tool present but command
    could not be executed" and left off. A working linter, silently not
    configured, with a diagnostic that reads like a broken install.

    It never showed up on a developer machine with no globally-installed tools,
    which is exactly why it survived: the bug needs a venv AND a global tool at
    the same time, and the platform where that is normal is the one nobody was
    testing on.
    """
    if prefix and os.path.exists(os.path.join(root, prefix, binary)):
        return prefix
    if shutil.which(binary) is not None:
        return ""
    return None


def tool_exists(root, binary, prefix):
    """Does this tool exist at all? Used by --install to decide what to fetch;
    the DETECTION pass wants tool_prefix, because it has to spell the command."""
    return tool_prefix(root, binary, prefix) is not None


# ── droppings ────────────────────────────────────────────────────────────────
# Real tools run against real code write caches, and they write them INTO THE
# PROJECT. That collides with two promises this script makes:
#
#   1. Report mode ends with "report only — nothing changed", and setup.sh's
#      --dry-run says "nothing will be written". Both were false the moment a
#      probe ran: asking a type checker "would this pass?" left a .mypy_cache/
#      behind. A question must not have a side effect, least of all one whose
#      absence was just promised in writing.
#   2. The user's next `git status` must not show junk they did not create. They
#      have just been told to run `git add -A && git commit`, and a novice will
#      do exactly that — committing a binary cache they cannot explain.
#
# So probing is made EPHEMERAL: every cache location we know how to redirect is
# pointed at a scratch directory that is deleted afterwards. This is best-effort
# by nature — an unknown tool will still write whatever it likes — which is why
# it is paired with CACHE_ARTIFACTS below rather than relied on alone.
def probe_env(cache_root):
    """Environment for a probe: same as ours, with tool caches sent to scratch."""
    e = dict(os.environ)
    for var, sub in (("MYPY_CACHE_DIR", "mypy"),
                     ("RUFF_CACHE_DIR", "ruff"),
                     ("BLACK_CACHE_DIR", "black"),
                     ("PYTHONPYCACHEPREFIX", "pycache"),
                     ("npm_config_cache", "npm")):
        e[var] = os.path.join(cache_root, sub)
    # COVERAGE_FILE names a FILE, not a directory — pointing it at one would make
    # coverage fail to write rather than write elsewhere.
    e["COVERAGE_FILE"] = os.path.join(cache_root, "coverage.data")
    # pytest's .pytest_cache has no env var; the plugin can only be switched off.
    # Appended, never replaced: a project may already be passing options here.
    e["PYTEST_ADDOPTS"] = (e.get("PYTEST_ADDOPTS", "")
                           + " -p no:cacheprovider").strip()
    return e


# What each tool leaves in a project WHEN THE GATE RUNS FOR REAL. Redirecting a
# probe fixes detection; it does nothing about the fact that a configured
# TYPECHECK_CMD re-creates that cache on every single feature build from then on.
# So configuring a gate also means taking responsibility for its droppings.
#
# This is the one file allowed to name tools, which is exactly why the knowledge
# lives here and not in install.sh: gates.sh, rocket.sh and install.sh stay
# stack-agnostic, and a project's .gitignore still ends up correct for its stack.
CACHE_ARTIFACTS = {
    "mypy":     [".mypy_cache/"],
    "ruff":     [".ruff_cache/"],
    "pytest":   [".pytest_cache/", ".coverage", ".coverage.*", "htmlcov/"],
    "pyright":  [],
    "flake8":   [],
    "pylint":   [],
    "eslint":   [".eslintcache"],
    "tsc":      ["*.tsbuildinfo"],
    "jest":     [],
    "prettier": [],
    "cargo":    ["target/"],
    "go":       [],
    "gofmt":    [],
    "golangci-lint": [],
    "gitleaks": [],
    "trufflehog": [],
    "npm":      [],
    "pip-audit": [],
    "black":    [],          # caches under the user's XDG dir, not the project
    "cargo-audit": [],
    "cargo-tarpaulin": ["target/"],
    "govulncheck": [],
}

# Running any of these at all means the interpreter writes bytecode next to the
# project's own source, which is the single most common piece of unexplained
# junk in a Python repo's git status.
ECOSYSTEM_ARTIFACTS = {
    "python": ["__pycache__/", "*.py[cod]"],
    "node":   [],
    "go":     [],
    "rust":   [],
}


def ignore_droppings(root, patterns):
    """Add cache patterns to .gitignore, once, never removing anything.

    Appends only what is missing — re-running detection must not grow the file,
    and the user's own entries are never touched. Deliberately does nothing when
    there is no repo and no .gitignore: creating one in a plain directory is a
    decision that is not ours to make.
    """
    gi = os.path.join(root, ".gitignore")
    if not os.path.exists(gi) and not os.path.isdir(os.path.join(root, ".git")):
        return []
    existing = set()
    if os.path.exists(gi):
        with open(gi) as fh:
            existing = {l.strip() for l in fh if l.strip()}
    new = [p for p in patterns if p not in existing]
    if not new:
        return []
    with open(gi, "a") as fh:
        fh.write("\n# Caches written by the checks rocket-loop configured for you.\n")
        for p in new:
            fh.write(p + "\n")
    return new


def probe(root, cmd, env=None):
    """Run a candidate gate against the CURRENT code.

    Returns (state, detail, output): "clean" | "dirty" | "error" | "timeout".
    'dirty' is the interesting one — the tool works fine, the code does not pass
    it. That is pre-existing debt and must not become a gate today.

    `output` is returned because exit code alone is not always the whole verdict:
    the coverage gate additionally requires a parseable percentage, and a gate
    that exits 0 while measuring nothing is a gate that FAILS at run time.
    """
    try:
        p = subprocess.run(cmd, shell=True, cwd=root, capture_output=True,
                           text=True, timeout=PROBE_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return "timeout", "exceeded {}s".format(PROBE_TIMEOUT), ""
    except Exception as e:                                    # noqa: BLE001
        return "error", str(e), ""
    out = _ANSI.sub("", p.stdout + p.stderr)
    if p.returncode in (126, 127):
        return "error", "command could not be executed", out
    if p.returncode == 0:
        return "clean", "", out

    # Tools colourise their output; raw escape codes turn the report line into
    # unreadable junk like "└ [0m". Strip them, then take the last line that
    # actually says something.
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    detail = lines[-1][:120] if lines else "exit {}".format(p.returncode)

    # A nonzero exit is NOT automatically "your code is dirty". The tool may
    # have rejected the command line itself — the classic case being a coverage
    # run where the base runner exists but the coverage PLUGIN does not, so the
    # Distinguish "the tool rejected its own arguments" from "the tool worked and
    # your code failed". Getting this backwards is expensive in both directions:
    # it either sends someone to reinstall a linter that was fine, or tells them
    # their code fails a check that never actually ran.
    #
    # LENGTH is the reliable signal, not vocabulary. A tool that cannot parse its
    # command line says so in a few lines and exits; a tool that found real
    # problems prints a report. Matching on words alone failed both ways — a
    # 500-finding lint report will eventually contain the word "usage:"
    # somewhere, and that is exactly how a project with genuine debt got told its
    # linter was broken. So a usage marker only counts when the output is also
    # short enough to BE a usage message.
    edges = "\n".join(lines[:3] + lines[-3:]).lower()
    usage_markers = ("unrecognized argument", "unrecognized option", "no such option",
                     "unknown option", "unknown flag", "usage:")
    looks_like_usage = len(lines) <= 15 and any(m in edges for m in usage_markers)
    if p.returncode == 4 or looks_like_usage:      # pytest reserves 4 for usage
        return "unusable", detail, out

    return "dirty", detail, out


def main():
    ap = argparse.ArgumentParser(description="Detect and configure quality gates")
    ap.add_argument("--root", default=".")
    ap.add_argument("--write", action="store_true",
                    help="patch rocket.config.sh (default: report only)")
    ap.add_argument("--no-probe", action="store_true",
                    help="skip running each gate against the current code")
    ap.add_argument("--install", action="store_true",
                    help="install missing project-scoped tools first, then detect")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    ecos = detect_ecosystems(root)
    prefix = venv_prefix(root)

    # Somewhere for the probes' caches to go that is NOT the user's project.
    # Created up front and removed in the `finally` below, so even a crash mid
    # detection does not strand a directory anywhere.
    cache_root = tempfile.mkdtemp(prefix="rocket-probe-")
    penv = probe_env(cache_root)
    try:
        return _run(args, root, ecos, prefix, penv)
    finally:
        shutil.rmtree(cache_root, ignore_errors=True)


def _run(args, root, ecos, prefix, penv):

    if not ecos:
        print("No recognised project markers here (pyproject.toml, package.json,")
        print("go.mod, Cargo.toml…). Nothing to configure — gates stay off, which")
        print("is safe: gates.sh skips every gate that has no command.")
        return 0

    # ── optionally install what is missing, BEFORE detecting ─────────────────
    # Ordering matters: install first, then the normal detection pass sees the
    # new tools and probes them like any other. That keeps one code path for
    # "does this tool exist and does the code pass it", instead of a second,
    # more optimistic one for freshly-installed tools.
    if args.install:
        wanted, machine_level = [], []
        for eco in ecos:
            pools = dict(CANDIDATES.get(eco, {}))
            pools["test"] = TEST_CANDIDATES.get(eco, [])   # the runner matters most
            for gate, cands in pools.items():
                for binary, _t, hint in cands:
                    if tool_exists(root, binary, prefix) or hint.startswith("("):
                        break          # already have it, or it ships with the runtime
                    cmd = install_cmd_for(root, hint, prefix)
                    if cmd:
                        wanted.append(cmd)
                    else:
                        machine_level.append((binary, hint))
                    break              # only ever install the FIRST choice per gate

        # Same package can serve several gates (ruff is format AND lint).
        seen, uniq = set(), []
        for c in wanted:
            if c not in seen:
                seen.add(c)
                uniq.append(c)

        if uniq:
            print("Installing project-scoped tools ({} command(s))…".format(len(uniq)))
            for c in uniq:
                print("  $ {}".format(c))
                r = subprocess.run(c, shell=True, cwd=root,
                                   capture_output=True, text=True, timeout=600)
                if r.returncode != 0:
                    # Not fatal. A tool that fails to install simply stays
                    # missing, and detection below will report it as such —
                    # which is exactly the state we were already handling.
                    tail = (r.stdout + r.stderr).strip().splitlines()
                    print("    ✗ failed: {}".format(tail[-1][:100] if tail else "see above"))
                else:
                    print("    ✓")
            print()
        if machine_level:
            # Deliberately not run. These change the machine, not the project.
            print("Not installed automatically — these change your machine, not just")
            print("this project, so they are your call:")
            for b, h in machine_level:
                print("    {:<14} {}".format(b, h))
            print()

    enabled, missing, debt, notes = {}, [], [], []
    chosen_tools = []      # every tool we actually RAN or configured

    for gate in ("test", "format", "lint", "typecheck", "coverage", "secrets", "depaudit"):
        chosen = None
        chosen_bin = None
        chosen_install = None
        chosen_at = ""          # the prefix the tool was actually FOUND at
        hint = None
        for eco in ecos:
            pool = (TEST_CANDIDATES.get(eco, []) if gate == "test"
                    else CANDIDATES.get(eco, {}).get(gate, []))
            for binary, tmpl, install in pool:
                # Build the command from where the tool IS, not from where the
                # project's venv would be if it had one. See tool_prefix().
                where = tool_prefix(root, binary, prefix)
                if where is not None:
                    chosen = tmpl.format(r=where) + exclusions_for(binary)
                    chosen_bin, chosen_install, chosen_at = binary, install, where
                    if binary not in chosen_tools:
                        chosen_tools.append(binary)
                    break
                if hint is None:
                    hint = (gate, binary, install)
            if chosen:
                break
        if not chosen:
            if hint:
                missing.append(hint)
            continue

        if args.no_probe:
            enabled[gate] = chosen
            continue

        # THE FORMATTER MUST NOT BE PROBED WITH THE COMMAND WE CONFIGURE.
        # A formatter's whole job is to rewrite files. Running `ruff format .`
        # here to "see if it passes" silently reformats the entire codebase —
        # during a command whose output literally ends with "report only,
        # nothing changed". That is a destructive lie, and it happened. Probe
        # with the tool's check/dry-run mode; configure the real one.
        probe_cmd = chosen
        if gate == "format":
            probe_cmd = FORMAT_PROBE.get(chosen_bin, "")
            if probe_cmd:
                # The probe must exclude exactly what the real command excludes,
                # or detection decides "dirty" from files the gate would never
                # look at — and defers a gate that would in fact have passed.
                probe_cmd = probe_cmd.format(r=chosen_at) + exclusions_for(chosen_bin)
            if not probe_cmd:
                # No known dry-run form. Enable without probing rather than run
                # a mutating command — format never blocks the verdict anyway,
                # so a wrong guess here costs nothing.
                enabled[gate] = chosen
                continue

        state, detail, out = probe(root, probe_cmd, env=penv)

        # EXIT 0 IS NOT ENOUGH FOR COVERAGE. gates.sh will not accept a coverage
        # run that printed no percentage — "no percent sign in the output =
        # nothing was measured = FAIL" — so a runner that exits 0 while measuring
        # nothing (the coverage plugin quietly absent, a wrapper that swallows
        # the report) gets configured here and then FAILS the very first gate run
        # of a freshly set-up project, on a command nobody chose. Detection has
        # to apply the same acceptance test the gate does.
        if gate == "coverage" and state == "clean" and "%" not in out:
            missing.append((gate, chosen_bin, chosen_install))
            notes.append("coverage: {} exits 0 but prints no percentage, so the "
                         "coverage gate would report FAIL (nothing measured). Left "
                         "OFF. Usually the coverage plugin is not installed: {}"
                         .format(chosen_bin, chosen_install))
            continue

        if state == "clean":
            enabled[gate] = chosen
        elif state == "dirty" and gate == "test":
            # TEST_CMD is NOT just a gate — it is threaded into every agent
            # prompt, the stop hook, and the baseline recorder. Leaving it blank
            # because the suite is currently red would take the test command away
            # from the builders too, and a builder that does not know how to run
            # the tests is far worse than one facing a red suite. So it is always
            # configured when a working runner exists; the red suite is reported
            # as debt, loudly, but the setting stays.
            enabled[gate] = chosen
            debt.append((gate, chosen, detail + "  [configured anyway — see note]"))
        elif state == "dirty":
            # The whole adoption story lives in this branch.
            debt.append((gate, chosen, detail))
        elif state == "unusable":
            # The runner is there but a piece it needs is not — report the
            # INSTALL that fixes it, not the code.
            missing.append((gate, chosen_bin, chosen_install))
        else:
            missing.append((gate, chosen_bin, "tool present but {}".format(detail)))

    if args.as_json:
        json.dump({"ecosystems": ecos, "venv_prefix": prefix, "enabled": enabled,
                   "debt": [{"gate": g, "cmd": c, "detail": d} for g, c, d in debt],
                   "missing": [{"gate": g, "tool": t, "install": i} for g, t, i in missing],
                   "notes": notes},
                  sys.stdout, indent=2)
        print()
        return 0

    print("═" * 70)
    print("GATE DETECTION — {}".format(root))
    print("  project looks like: {}{}".format(
        " + ".join(ecos), "   (tools in {})".format(prefix) if prefix else ""))
    print("═" * 70)

    if enabled:
        print("\n✓ TURNING ON — these run clean against your code today")
        for g, c in enabled.items():
            print("    {:<10} {}".format(g, c))

    if debt:
        print("\n⏸ LEAVING OFF — the tool works, your existing code does not pass it")
        print("  This is a backlog, not a gate. Switching these on now would block")
        print("  every new feature on problems that predate it.")
        for g, c, d in debt:
            print("    {:<10} {}".format(g, c))
            print("    {:<10}   └ {}".format("", d))
        print("  Written to GATE_DEBT.md. Clear one, re-run this, and it turns on.")

    if missing:
        print("\n○ NOT AVAILABLE — tool isn't installed")
        print("  Deliberately NOT configured: a command that cannot run counts as a")
        print("  FAILED gate here, so guessing would block everything.")
        for g, t, i in missing:
            print("    {:<10} {:<14} install: {}".format(g, t, i))

    if missing:
        # The single most useful line in the whole report: what to actually type.
        # Grouping by installer means one paste, not six.
        #
        # Only REAL install hints may become a command. The `missing` list also
        # carries diagnostics ("tool present but <reason>"), and splitting one of
        # those on whitespace produced a paste-able line reading
        # `tool present executed` — a command that does not exist, printed under
        # "To turn those on, run:". Telling somebody to run a nonsense command is
        # worse than telling them nothing.
        groups = {}
        for _g, _t, inst in missing:
            if not any(inst.startswith(k) for k in INSTALLERS):
                continue
            groups.setdefault(inst.split()[0] + " " + inst.split()[1], set()).add(
                inst.split()[-1])
        if groups:
            print("\n  To turn those on, run:")
            for base, pkgs in groups.items():
                print("      {} {}".format(base, " ".join(sorted(pkgs))))
            print("  …then re-run this with --write.")

    if notes:
        print("\n! WORTH KNOWING")
        for n in notes:
            print("    {}".format(n))

    if not enabled and not debt:
        print("\nNo gates could be enabled. The test gate (TEST_CMD) still applies.")

    if not args.write:
        print("\n(report only — nothing changed. Re-run with --write to apply.)")
        return 0

    # ── apply ────────────────────────────────────────────────────────────────
    cfg = os.path.join(root, "rocket.config.sh")
    if not os.path.exists(cfg):
        print("\n✗ no rocket.config.sh here — run install.sh first.")
        return 2

    with open(cfg) as fh:
        text = fh.read()

    for gate, cmd in enabled.items():
        key = CONFIG_KEY[gate]
        line = '{}="{}"'.format(key, cmd.replace('"', '\\"'))
        pat = re.compile(r'^{}=.*$'.format(key), re.M)
        text = pat.sub(line, text) if pat.search(text) else text + "\n" + line + "\n"

    # A coverage command with no threshold is a FAIL by design, so setting one
    # without the other would break the very gate we just turned on.
    if "coverage" in enabled and not re.search(r'^COVERAGE_MIN=\s*[1-9]', text, re.M):
        if re.search(r'^COVERAGE_MIN=.*$', text, re.M):
            text = re.sub(r'^COVERAGE_MIN=.*$', "COVERAGE_MIN=60", text, flags=re.M)
        else:
            text += "\nCOVERAGE_MIN=60\n"

    with open(cfg, "w") as fh:
        fh.write(text)
    print("\n✓ wrote {} gate(s) to rocket.config.sh".format(len(enabled)))

    # A configured gate runs on every future build, and every run re-creates the
    # tool's cache in the project. Redirecting the PROBE (probe_env) keeps
    # detection itself clean; it does nothing about that. Without this, the first
    # thing a user sees after being told to `git add -A && git commit` is a pile
    # of cache files they did not create, cannot explain, and will commit.
    droppings = []
    for eco in ecos:
        droppings += ECOSYSTEM_ARTIFACTS.get(eco, [])
    for b in chosen_tools:
        droppings += CACHE_ARTIFACTS.get(b, [])
    seen, uniq_drops = set(), []
    for d in droppings:
        if d not in seen:
            seen.add(d)
            uniq_drops.append(d)
    added = ignore_droppings(root, uniq_drops)
    if added:
        print("✓ added {} cache pattern(s) to .gitignore — the checks above write"
              .format(len(added)))
        print("  these while they run, and they are not yours to look at.")

    if debt:
        with open(os.path.join(root, "GATE_DEBT.md"), "w") as fh:
            fh.write("# Gate debt\n\n")
            fh.write("Checks that WORK but that the existing code does not pass. They are\n")
            fh.write("switched off so they cannot block new features on old problems.\n")
            fh.write("Fix one, re-run `python3 scripts/detect_gates.py --write`, and it\n")
            fh.write("becomes a real gate from then on.\n\n")
            for g, c, d in debt:
                fh.write("## {}\n\n    {}\n\n{}\n\n".format(g, c, d))
        print("✓ wrote GATE_DEBT.md ({} deferred)".format(len(debt)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
