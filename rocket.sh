#!/usr/bin/env bash
# Rocket — Autonomous Feature Build Loop driver.
#
# Reads FEATURE_QUEUE.md, picks the first QUEUED feature whose Depends On
# entries are all SHIPPED, invokes .claude/commands/rocket.md against that
# brief, then re-reads the queue to decide whether to loop. Caps at
# MAX_FEATURES per invocation.
#
# Depends On grammar handled:
#   "—" | "-" | ""    → no dependencies
#   "ALL"             → every other row must be SHIPPED
#   "1, 2"            → all listed rows must be SHIPPED
#   "4, 5 or 6"       → comma-separated groups; within a group "or" means
#                       any single alternative satisfies it
#
# Exit codes:
#   0  all processed cleanly, or 3-feature cap hit, or queue exhausted
#      after at least one ship
#   1  a feature was attempted but did not reach SHIPPED (blocked)
#   2  no QUEUED work with met dependencies (nothing to do)

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

QUEUE_FILE="FEATURE_QUEUE.md"
ROCKET_PROMPT=".claude/commands/rocket.md"
MAX_FEATURES=3
features_processed=0

if [[ ! -f "$QUEUE_FILE" ]]; then
  echo "Rocket: ERROR — $QUEUE_FILE not found" >&2
  exit 2
fi

if [[ ! -f "$ROCKET_PROMPT" ]]; then
  echo "Rocket: ERROR — $ROCKET_PROMPT not found" >&2
  exit 2
fi

# pick_next_brief — emits one line:  <row#>\t<brief-path>\t<feature-slug>
# Empty stdout means no ready work.
pick_next_brief() {
  python3 - "$QUEUE_FILE" <<'PY'
import os, re, sys

path = sys.argv[1]
with open(path) as f:
    lines = f.readlines()

# Capture: # | brief | deps | complexity | status
row_re = re.compile(
    r'^\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*[^|]+?\s*\|\s*(\w+)\s*\|'
)

rows = []
for line in lines:
    m = row_re.match(line)
    if m:
        rows.append((int(m.group(1)), m.group(2).strip(),
                     m.group(3).strip(), m.group(4).strip()))

shipped = {n for n, _, _, s in rows if s == "SHIPPED"}

def deps_met(raw, current_num):
    if raw in ("—", "-", ""):
        return True
    if raw.upper() == "ALL":
        return all(s == "SHIPPED" for n, _, _, s in rows if n != current_num)
    for group in raw.split(","):
        group = group.strip()
        alts = [a.strip() for a in re.split(r'\bor\b', group)]
        satisfied = False
        for a in alts:
            try:
                if int(a) in shipped:
                    satisfied = True
                    break
            except ValueError:
                continue
        if not satisfied:
            return False
    return True

for num, brief, deps, status in rows:
    if status != "QUEUED":
        continue
    if deps_met(deps, num):
        slug = os.path.splitext(os.path.basename(brief))[0]
        print(f"{num}\t{brief}\t{slug}")
        break
PY
}

# status_of_row — prints current status string for a given row number.
status_of_row() {
  python3 - "$QUEUE_FILE" "$1" <<'PY'
import re, sys
path, target = sys.argv[1], int(sys.argv[2])
row_re = re.compile(
    r'^\|\s*(\d+)\s*\|\s*[^|]+?\s*\|\s*[^|]+?\s*\|\s*[^|]+?\s*\|\s*(\w+)\s*\|'
)
with open(path) as f:
    for line in f:
        m = row_re.match(line)
        if m and int(m.group(1)) == target:
            print(m.group(2).strip())
            break
PY
}

while (( features_processed < MAX_FEATURES )); do
  pick_output="$(pick_next_brief)"

  if [[ -z "$pick_output" ]]; then
    if (( features_processed == 0 )); then
      echo "Rocket: no QUEUED work with met dependencies"
      exit 2
    fi
    echo "Rocket: queue exhausted | $features_processed/$MAX_FEATURES processed"
    exit 0
  fi

  row_num="$(printf '%s\n' "$pick_output" | cut -f1)"
  brief_path="$(printf '%s\n' "$pick_output" | cut -f2)"
  feature_name="$(printf '%s\n' "$pick_output" | cut -f3)"

  echo "Rocket: starting $feature_name (row $row_num, brief $brief_path)"

  # Snapshot the green test set so qa-gate can distinguish regressions
  # from new failures during the build.
  bash .claude/hooks/record-baseline.sh

  claude -p "$(cat "$ROCKET_PROMPT")" --arg feature_brief="$brief_path"

  status_after="$(status_of_row "$row_num")"
  features_processed=$((features_processed + 1))

  if [[ "$status_after" == "SHIPPED" ]]; then
    echo "Rocket: shipped $feature_name | $features_processed/$MAX_FEATURES processed"
  else
    echo "Rocket: BLOCKED $feature_name | $features_processed/$MAX_FEATURES processed"
    exit 1
  fi
done

echo "Rocket: cap reached | $features_processed/$MAX_FEATURES processed"
exit 0
