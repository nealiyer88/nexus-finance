#!/usr/bin/env bash
# ── bg.sh — detached launcher for rocket.sh ──────────────────────────────────
# Runs the loop unattended as a PLAIN DETACHED BASH PROCESS, deliberately NOT
# inside a chat session. A session babysitting a multi-hour bash loop burns
# context on output it will never act on, and adds a second thing that can die
# (lose the session, lose the run). A detached process outlives the terminal,
# the editor, and the laptop lid; its only interface is a log file.
#
# Usage:
#   bg.sh start [rocket.sh args...]   launch detached, verify it really started
#   bg.sh status                      running? how long? last progress lines
#   bg.sh tail [-n N]                 follow the live log (or print the tail)
#   bg.sh stop                        TERM the process group, then KILL
#   bg.sh logs                        past run logs, newest last
#
# Portability: this must run on macOS bash 3.2 as well as Linux, so no GNU-only
# flags (no `ps --pid`, no `stat -c`, no `timeout`), no bash 4 syntax.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

# rocket.sh is resolved next to THIS script, but the run's cwd is left alone:
# rocket.sh reads/writes features/ relative to the directory it is invoked
# from, so cd-ing here would silently retarget the run at another project.
SELF_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
ROCKET_SH="${ROCKET_SH:-$SELF_DIR/rocket.sh}"

# State lives under the invoking project's .rocket/ (already gitignored, and
# already where rocket's fan-out worktrees go) — not next to the script, so a
# shared install cannot cross-wire two projects' runs.
BG_DIR="${ROCKET_BG_DIR:-$PWD/.rocket/bg}"
PIDFILE="$BG_DIR/rocket.pid"
CURFILE="$BG_DIR/rocket.current"   # log path of the run that PIDFILE describes
STARTFILE="$BG_DIR/rocket.start"   # epoch seconds, for status's elapsed time

GRACE_SECONDS="${ROCKET_BG_GRACE:-15}"   # TERM → KILL window in `stop`

die() { echo "bg: $*" >&2; exit 1; }

# A PID alone is not proof: PIDs are recycled, and after a reboot the number in
# a leftover pidfile can belong to anything. Every liveness check therefore also
# confirms the command line still looks like the script we launched.
#
# It matches the BASENAME OF $ROCKET_SH, not a hardcoded "rocket.sh". The path is
# an override this script advertises, so hardcoding the name made the check
# disagree with the launch: point ROCKET_SH at anything not literally named
# rocket.sh and `start` reported "exited immediately" for a process that was
# running fine, then `stop` refused to kill it. A liveness check that calls a
# live process dead is worse than no check — it strands a real run.
ROCKET_NAME="$(basename -- "$ROCKET_SH")"
pid_is_rocket() {  # <pid>
    local pid="$1" cmd
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    # -ww: report the FULL command line. Linux procps truncates `command` to the
    # terminal width — 80 columns when stdout is not a tty, which is exactly how
    # this runs — so a project nested deep enough pushes the script name past the
    # cut and the match fails for a healthy process. Same class of stranded-run
    # bug as the hardcoded name above, just triggered by path length instead.
    cmd=$(ps -ww -p "$pid" -o command= 2>/dev/null)
    case "$cmd" in
        *"$ROCKET_NAME"*) return 0 ;;
        *)                return 1 ;;
    esac
}

# Is this pid a live process AT ALL — no question of what it is running.
#
# THIS IS NOT pid_is_rocket, AND THE DIFFERENCE IS LOad-BEARING. pid_is_rocket
# additionally demands that the command line name the script, which is right when
# validating a pidfile written by a PREVIOUS invocation (pids get recycled; after
# a reboot the number in a leftover pidfile can belong to anything). It is wrong
# immediately after we fork the child ourselves: between the fork and the child's
# exec, `ps` still reports the LAUNCHER's command line. `$!` is our child by
# construction there, so liveness is `kill -0` and nothing else.
#
# Getting this wrong was not cosmetic. The start-time poll ran with zero delay
# after the fork, so whenever it landed in that pre-exec window it declared a
# perfectly healthy run dead — and then DELETED THE PIDFILE and exited nonzero.
# The run kept going, untracked. The next `bg.sh start` saw no pidfile, happily
# launched a second loop, and two runs built the same repo at once, fighting over
# the queue and the working tree. That is precisely the disaster the
# single-instance lock exists to prevent, caused by the lock's own liveness
# check. It was timing-dependent, so it passed on one machine and failed on
# another rather than failing honestly everywhere.
pid_alive() {  # <pid>
    local pid="$1" st
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    # A process that has exited but not yet been reaped still answers `kill -0`.
    # Only a Z state proves that. Anything else — including ps being unable to
    # tell us — counts as alive: `kill -0` is the authoritative POSIX check, and
    # an unreadable ps must not be allowed to manufacture a death.
    st=$(ps -p "$pid" -o state= 2>/dev/null | tr -d '[:space:]')
    case "$st" in
        Z*) return 1 ;;
        *)  return 0 ;;
    esac
}

# Reads PIDFILE and echoes the pid ONLY if it is a live rocket. A pidfile whose
# process is gone is cleared here rather than refusing to start forever — a hard
# kill or a reboot must not permanently wedge the launcher.
live_pid() {
    local pid
    [ -f "$PIDFILE" ] || return 1
    pid=$(head -1 "$PIDFILE" 2>/dev/null | tr -dc '0-9')
    if pid_is_rocket "$pid"; then
        echo "$pid"
        return 0
    fi
    rm -f "$PIDFILE" "$STARTFILE" 2>/dev/null || true
    return 1
}

human_elapsed() {  # <seconds>
    local s="$1"
    printf '%dh%02dm%02ds' $((s / 3600)) $(((s % 3600) / 60)) $((s % 60))
}

cmd_start() {
    local existing
    if existing=$(live_pid); then
        echo "bg: rocket is already running (pid $existing)." >&2
        echo "    Use 'bg.sh status' to check on it, 'bg.sh stop' to end it." >&2
        exit 1
    fi

    [ -f "$ROCKET_SH" ] || die "no rocket.sh at $ROCKET_SH (set ROCKET_SH= to override)"
    mkdir -p -- "$BG_DIR" || die "cannot create $BG_DIR"

    local log="$BG_DIR/rocket-$(date +%Y%m%d-%H%M%S).log"

    # Monitor mode puts the background job in a process group of its OWN whose
    # pgid equals its pid — that is what makes `kill -TERM -$pid` in stop reach
    # the agent subprocesses too. setsid would do the same but does not exist on
    # macOS. nohup + disown is what survives the terminal closing.
    # DELIBERATELY NOT DISOWNED YET. Staying this shell's child for the length of
    # the startup check is what lets `wait` below report the run's ACTUAL exit
    # status instead of us inferring it. nohup already covers the only thing
    # disown is needed for (SIGHUP when the terminal goes away), and this shell
    # is alive throughout the check, so nothing is at risk in the meantime.
    set -m
    nohup bash "$ROCKET_SH" "$@" >"$log" 2>&1 &
    local pid=$!
    set +m

    echo "$pid" > "$PIDFILE"
    echo "$log" > "$CURFILE"
    date +%s > "$STARTFILE"

    # THE failure this launcher exists to catch: a run that dies in the first
    # seconds (bad args, dirty tree, lock held, missing config) while the
    # launcher cheerfully prints a pid.
    #
    # THE ONLY EARLY EXIT IS DEATH. This loop used to also break the moment the
    # process was alive AND had written something, so that a healthy start
    # returned fast — but "alive right now" is not evidence that anything
    # started. A script that writes to stderr and then exits satisfies both
    # conditions in the same instant, so the launcher reported success for a run
    # that was already dying. That is the worse direction of the two: the user
    # walks away believing a build is running, comes back to nothing, and there
    # is no error anywhere.
    #
    # The old code hid it. Its liveness test was pid_is_rocket, which is false
    # during the child's pre-exec window, so `alive` was 0 and it reported death
    # — accidentally right for dead processes and wrong for healthy ones. Making
    # `alive` truthful is what exposed the success criterion as unsound.
    #
    # Surviving the window is the only honest evidence, and there is no way to
    # learn it faster than the window is long. The poll count is unchanged: it is
    # a settling period, not a number to tune until a test goes green.
    local i=0 alive=1
    while [ $i -lt 25 ]; do
        if ! pid_alive "$pid"; then alive=0; break; fi
        sleep 0.2
        i=$((i + 1))
    done

    if [ $alive -eq 0 ]; then
        # It is gone. GUESSING why from the log would be the same mistake again,
        # so read the real exit status — which is available precisely because we
        # have not disowned it yet. A short run that finished cleanly (an empty
        # queue drains in well under a second) is a SUCCESS, and reporting it as
        # "died on launch" would be a false alarm on the happiest path there is.
        local ec=0
        wait "$pid" 2>/dev/null
        ec=$?
        rm -f "$PIDFILE" "$STARTFILE" 2>/dev/null || true
        if [ "$ec" -eq 0 ]; then
            echo "bg: rocket finished during startup (exit 0) — nothing left to do."
            echo "    That is a completed run, not a failure. Log: $log"
            if [ -s "$log" ]; then echo "──── last 20 lines ────"; tail -n 20 "$log"; fi
            return 0
        fi
        echo "bg: rocket exited immediately after launch (exit $ec) — NOT running." >&2
        echo "──── last 40 lines of $log ────" >&2
        # An empty log is itself the diagnosis (died before printing anything —
        # usually a bad interpreter or a nonexistent flag), so say so rather
        # than showing a blank block that reads like the tail failed.
        if [ -s "$log" ]; then tail -n 40 "$log" >&2; else echo "(log is empty — process produced no output at all)" >&2; fi
        exit 1
    fi

    # Survived the window, so it is a real run: now detach it for good.
    disown "$pid" 2>/dev/null || disown 2>/dev/null || true

    echo "bg: rocket started (pid $pid, pgid $pid)"
    echo "    log:    $log"
    echo "    follow: bg.sh tail    stop: bg.sh stop"
    # Read the log directly rather than carrying a flag from the poll. The flag
    # was the same "output means something" reasoning that made the startup check
    # unsound; here it is only a diagnostic, but there is no reason to keep a
    # second copy of the fact when the file is right there.
    if [ ! -s "$log" ]; then
        # Alive but silent. Not fatal — a run can spend its first seconds in git
        # or model setup before printing — but worth flagging, since a silent
        # process is also what a hung one looks like.
        echo "bg: NOTE — alive but no output yet; check 'bg.sh tail'." >&2
    fi
}

cmd_status() {
    local pid
    if ! pid=$(live_pid); then
        echo "bg: not running"
        [ -f "$CURFILE" ] && echo "    last log: $(cat "$CURFILE")"
        return 0
    fi
    local log elapsed=""
    log=$(cat "$CURFILE" 2>/dev/null)
    if [ -f "$STARTFILE" ]; then
        local started now
        started=$(cat "$STARTFILE" 2>/dev/null | tr -dc '0-9')
        now=$(date +%s)
        [ -n "$started" ] && elapsed=$(human_elapsed $((now - started)))
    fi
    echo "bg: RUNNING (pid $pid)${elapsed:+  uptime $elapsed}"
    echo "    log: ${log:-<unknown>}"
    if [ -n "$log" ] && [ -f "$log" ]; then
        echo "    ── last 10 lines ──"
        tail -n 10 "$log" | sed 's/^/    /'
    fi
}

cmd_tail() {
    local n=40
    while [ $# -gt 0 ]; do
        case "$1" in
            -n) n="${2:-40}"; shift 2 ;;
            -n*) n="${1#-n}"; shift ;;
            *) die "tail: unknown option '$1'" ;;
        esac
    done
    local log
    log=$(cat "$CURFILE" 2>/dev/null)
    [ -n "$log" ] && [ -f "$log" ] || die "no log to tail (has anything run? see 'bg.sh logs')"
    # Follow only while a run is live; on a finished run -f would just hang.
    if live_pid >/dev/null; then
        tail -n "$n" -f "$log"
    else
        echo "bg: not running — printing the tail of $log" >&2
        tail -n "$n" "$log"
    fi
}

cmd_stop() {
    local pid
    if ! pid=$(live_pid); then
        echo "bg: not running (nothing to stop)"
        return 0
    fi
    # Signal the process GROUP, not the pid: rocket spawns agent CLIs and git
    # children, and killing only the parent orphans them still holding the repo.
    # Fall back to the bare pid if the group signal is rejected (a run adopted
    # from an older bg.sh may not be its own group leader).
    kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true

    local waited=0
    while [ "$waited" -lt "$GRACE_SECONDS" ]; do
        pid_is_rocket "$pid" || {
            rm -f "$PIDFILE" "$STARTFILE" 2>/dev/null || true
            echo "bg: stopped pid $pid with SIGTERM after ${waited}s"
            return 0
        }
        sleep 1
        waited=$((waited + 1))
    done

    kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    sleep 1
    if pid_is_rocket "$pid"; then
        echo "bg: pid $pid SURVIVED SIGKILL — kill it by hand" >&2
        return 1
    fi
    rm -f "$PIDFILE" "$STARTFILE" 2>/dev/null || true
    echo "bg: stopped pid $pid with SIGKILL (ignored SIGTERM for ${GRACE_SECONDS}s)"
}

cmd_logs() {
    [ -d "$BG_DIR" ] || { echo "bg: no runs yet ($BG_DIR does not exist)"; return 0; }
    local found=0 f
    for f in "$BG_DIR"/rocket-*.log; do
        [ -f "$f" ] || continue
        found=1
        # `ls -l` rather than stat: `stat -c` (GNU) and `stat -f` (BSD) take
        # incompatible format strings, and ls -l prints size + mtime on both.
        ls -l "$f"
    done
    [ $found -eq 0 ] && echo "bg: no run logs in $BG_DIR"
    return 0
}

usage() {
    echo "usage: bg.sh <start [args...]|status|tail [-n N]|stop|logs>" >&2
    exit 2
}

SUB="${1:-}"
[ $# -gt 0 ] && shift
case "$SUB" in
    start)  cmd_start "$@" ;;
    status) cmd_status ;;
    tail)   cmd_tail "$@" ;;
    stop)   cmd_stop ;;
    logs)   cmd_logs ;;
    ""|-h|--help|help) usage ;;
    *) echo "bg: unknown subcommand '$SUB'" >&2; usage ;;
esac
