#!/usr/bin/env bash
# ── post-ship-pr.template.sh — open a PR per shipped feature ─────────────────
# A TEMPLATE. Copy it, edit it, then point your config at it:
#
#   cp .claude/hooks/post-ship-pr.template.sh .claude/hooks/post-ship-pr.sh
#   # in rocket.config.sh:
#   POST_SHIP_HOOK='bash .claude/hooks/post-ship-pr.sh "$SLUG"'
#
# It is a template rather than a wired-in feature because opening a PR means
# naming a forge — GitHub here, via `gh`. That name does not belong in the
# harness, which has to work for a project on GitLab, Gitea, or no forge at
# all. Everything the harness itself does stays forge-agnostic; this is the
# seam where a project says which one it uses.
#
# WHAT IT ASSUMES, and what happens when the assumption is wrong:
#   - `gh` installed and authenticated. If not: this logs why and exits 0.
#   - A remote to push to. If not: logs and exits 0.
#   - The feature was shipped on a branch that is not the PR base. If rocket
#     shipped straight onto the base branch, there is nothing to open a PR
#     from; it says so and exits 0.
#
# EXIT 0 ON EVERY FAILURE PATH, deliberately. rocket.sh runs the post-ship hook
# AFTER the feature is shipped, committed, and flipped in the queue. The work is
# already done and correct. A forge being unreachable is a publishing problem,
# not a build problem, and must not be reported in a way that makes a good ship
# look like a bad one. Everything lands in features/_logs/post-ship-<slug>.log.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

SLUG="${1:-}"
[ -z "$SLUG" ] && { echo "post-ship-pr: no slug argument; nothing to do"; exit 0; }

# Where PRs target. Keep it in sync with ROCKET_BRANCH_DEFAULT in rocket.config.sh.
BASE_BRANCH="${PR_BASE_BRANCH:-main}"
REMOTE="${PR_REMOTE:-origin}"
LOG_DIR="${LOG_DIR:-features/_logs}"

say() { echo "post-ship-pr[$SLUG]: $*"; }

command -v gh >/dev/null 2>&1 || { say "gh not installed — skipping PR"; exit 0; }
gh auth status >/dev/null 2>&1 || { say "gh not authenticated — skipping PR"; exit 0; }
git remote get-url "$REMOTE" >/dev/null 2>&1 || { say "no remote '$REMOTE' — skipping PR"; exit 0; }

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"
if [ -z "$BRANCH" ] || [ "$BRANCH" = "HEAD" ]; then
    # Not `A || B && C` — that parses as `(A || B) && C`, which happens to work
    # here but silently changes meaning the moment someone adds a third clause.
    say "detached HEAD — skipping PR"
    exit 0
fi
if [ "$BRANCH" = "$BASE_BRANCH" ]; then
    say "shipped directly onto '$BASE_BRANCH' — no branch to open a PR from"
    exit 0
fi

git push -u "$REMOTE" "$BRANCH" >/dev/null 2>&1 || { say "push failed — skipping PR"; exit 0; }

if gh pr view "$BRANCH" >/dev/null 2>&1; then
    say "PR already open for '$BRANCH' — the new commits are already on it"
    exit 0
fi

# The PR body carries the EVIDENCE, not a description of the feature — the brief
# already describes the feature, and a reviewer's real question is "what did the
# automation actually check before it decided this was done?". Gate reports and
# review verdicts answer that; a prose summary does not. Only the per-gate and
# VERDICT lines go in, never raw tool logs: those stay on disk, and a PR body
# stuffed with a 40k-line test dump is a PR nobody reads.
BODY_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE"' EXIT
{
    echo "Automated build — feature \`$SLUG\`."
    echo
    echo "## Gate results"
    for scope in pre-merge post-integration; do
        f="$LOG_DIR/${SLUG}-gates-${scope}.md"
        if [ -f "$f" ]; then
            echo
            echo "### $scope"
            echo '```'
            grep -aE '^GATE |^VERDICT: ' "$f" 2>/dev/null || echo "(no gate lines)"
            echo '```'
        fi
    done
    echo
    echo "## Review verdicts"
    for kind in qa code exercise; do
        f="$LOG_DIR/${SLUG}-${kind}-verdict.md"
        if [ -f "$f" ]; then
            v="$(grep -aoE 'VERDICT: (PASS|FAIL|SKIPPED)' "$f" | tail -1)"
            echo "- **${kind}**: ${v:-no verdict line found}"
        else
            # Absent is reported, not omitted. A missing verdict file and a PASS
            # look identical in a list that only prints what it found.
            echo "- **${kind}**: no verdict file (${f})"
        fi
    done
    echo
    echo "Full gate logs and reports are in \`$LOG_DIR/\` on the build machine — "
    echo "deliberately not inlined here."
} > "$BODY_FILE"

if gh pr create --base "$BASE_BRANCH" --head "$BRANCH" \
     --title "$SLUG" --body-file "$BODY_FILE" >/dev/null 2>&1; then
    say "opened PR for '$BRANCH' → '$BASE_BRANCH'"
else
    say "gh pr create failed — feature is shipped and committed regardless"
fi
exit 0
