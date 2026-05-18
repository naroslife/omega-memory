# Shared helpers for scripts/sync-omega.sh and scripts/merge-omega.sh.
#
# Source this file from a host script that already declared REMOTE_HOST
# (and, for sync, OMEGA_LOCAL_HOME).  Functions write user-facing diagnostics
# to stderr so they don't pollute rsync/scp progress lines.
#
# Resilience model:
#   - SSH and scp failures retry 3x with 2/4/8s backoff (transient network).
#   - rsync failures retry 3x with --partial so interrupted transfers resume.
#   - "database is locked" failures from `omega import` retry 3x with 5/10/20s
#     backoff (a writer is holding the WAL; wait it out).
#   - Pre-flight checks (`preflight_local_db`, `preflight_remote_db`) refuse to
#     run unless ALLOW_ACTIVE=1 when a live MCP server is detected.

log()  { echo "[$(date +%H:%M:%S)] $*" >&2; }
warn() { echo "[$(date +%H:%M:%S)] WARN: $*" >&2; }
die()  { echo "[$(date +%H:%M:%S)] ERROR: $*" >&2; exit 1; }

# retry MAX_ATTEMPTS CMD ARGS...
retry() {
    local max=$1; shift
    local attempt=1
    local delay=2
    while true; do
        if "$@"; then
            return 0
        fi
        if [ "$attempt" -ge "$max" ]; then
            warn "command failed after $max attempts: $*"
            return 1
        fi
        warn "attempt $attempt/$max failed, retrying in ${delay}s: $*"
        sleep "$delay"
        attempt=$((attempt + 1))
        delay=$((delay * 2))
    done
}

ssh_retry() {
    retry 3 ssh -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
        -o ConnectTimeout=10 "$@"
}

scp_retry() {
    retry 3 scp -o ServerAliveInterval=15 -o ConnectTimeout=10 "$@"
}

# Pass full rsync arg list; the wrapper adds --partial / --partial-dir so
# interrupted transfers resume on retry.
rsync_retry() {
    retry 3 rsync --partial --partial-dir=.rsync-partial "$@"
}

# Print live MCP PIDs registered locally (one per line); exit 0 if any found,
# 1 otherwise.
local_mcp_pids() {
    local d="${OMEGA_LOCAL_HOME:-$HOME/.omega}/mcp_pids"
    [ -d "$d" ] || return 1
    local found=1 pid_file pid
    for pid_file in "$d"/*.pid; do
        [ -f "$pid_file" ] || continue
        pid="${pid_file##*/}"; pid="${pid%.pid}"
        if [ -n "$pid" ] && /bin/kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            found=0
        fi
    done
    return $found
}

# Same check on the remote side.  Echoes PIDs to stdout if any are alive.
remote_mcp_pids() {
    # shellcheck disable=SC2016,SC2029  # $HOME must expand on the remote side
    ssh_retry "$REMOTE_HOST" '
        d="$HOME/.omega/mcp_pids"
        [ -d "$d" ] || exit 1
        found=1
        for pf in "$d"/*.pid; do
            [ -f "$pf" ] || continue
            pid="${pf##*/}"; pid="${pid%.pid}"
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                echo "$pid"
                found=0
            fi
        done
        exit $found
    ' 2>/dev/null
}

# Run a command up to 3 times if its output mentions "database is locked".
# Captures stderr+stdout so we can pattern-match, then forwards on each attempt.
retry_on_db_lock() {
    local max=3
    local attempt=1
    local delay=5
    local out
    while true; do
        if out=$("$@" 2>&1); then
            [ -n "$out" ] && echo "$out"
            return 0
        fi
        echo "$out" >&2
        if echo "$out" | grep -qiE "database is locked|database is busy"; then
            if [ "$attempt" -ge "$max" ]; then
                warn "import still blocked after $max attempts; abort"
                return 1
            fi
            warn "DB lock contention (attempt $attempt/$max), waiting ${delay}s for writer..."
            sleep "$delay"
            attempt=$((attempt + 1))
            delay=$((delay * 2))
            continue
        fi
        # Non-lock failure — no point retrying.
        return 1
    done
}

# Pre-flight: abort if a live MCP server is detected locally, unless the caller
# opted in via ALLOW_ACTIVE=1.  Arg $1 = action description for the message.
preflight_local_db() {
    local action="$1"
    local pids
    pids=$(local_mcp_pids || true)
    if [ -z "$pids" ]; then
        return 0
    fi
    if [ "${ALLOW_ACTIVE:-0}" = "1" ]; then
        warn "local MCP server PIDs ($(echo "$pids" | tr '\n' ' ')) — proceeding under --allow-active"
        return 0
    fi
    warn "local MCP server(s) are running:"
    # shellcheck disable=SC2001
    echo "$pids" | sed 's/^/  PID /' >&2
    warn "they hold a write lock on omega.db and will block '$action'"
    warn "  - close the Claude Code session, OR"
    warn "  - pass --allow-active to retry on lock (slow but works)"
    return 1
}

preflight_remote_db() {
    local action="$1"
    local pids
    pids=$(remote_mcp_pids || true)
    if [ -z "$pids" ]; then
        return 0
    fi
    if [ "${ALLOW_ACTIVE:-0}" = "1" ]; then
        warn "remote MCP PIDs on $REMOTE_HOST ($(echo "$pids" | tr '\n' ' ')) — proceeding under --allow-active"
        return 0
    fi
    warn "remote MCP server(s) are running on $REMOTE_HOST:"
    # shellcheck disable=SC2001
    echo "$pids" | sed 's/^/  PID /' >&2
    warn "they hold a write lock on the remote omega.db and will block '$action'"
    warn "  - stop the remote Claude session, OR"
    warn "  - pass --allow-active"
    return 1
}

# Count rows in a local SQLite DB.  Echoes the integer to stdout, or empty on
# error.  Uses sqlite3 if present, else falls back to a Python one-liner.
sqlite_count_local() {
    local db="$1" table="${2:-memories}"
    if [ ! -f "$db" ]; then echo ""; return 1; fi
    if /usr/bin/command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$db" "SELECT COUNT(*) FROM $table;" 2>/dev/null
    else
        python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('SELECT COUNT(*) FROM '+sys.argv[2]).fetchone()[0])" "$db" "$table" 2>/dev/null
    fi
}

sqlite_count_remote() {
    local db="$1" table="${2:-memories}"
    # shellcheck disable=SC2016,SC2029  # remote-side expansion intentional
    ssh_retry "$REMOTE_HOST" "
        db=\"$db\" t=\"$table\"
        if [ ! -f \"\$db\" ]; then echo ''; exit 1; fi
        if command -v sqlite3 >/dev/null 2>&1; then
            sqlite3 \"\$db\" \"SELECT COUNT(*) FROM \$t;\" 2>/dev/null
        else
            python3 -c \"import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('SELECT COUNT(*) FROM '+sys.argv[2]).fetchone()[0])\" \"\$db\" \"\$t\" 2>/dev/null
        fi
    "
}

# sha256 of a local file, or empty if missing.
sha256_local() {
    [ -f "$1" ] || { echo ""; return; }
    /usr/bin/sha256sum "$1" 2>/dev/null | /usr/bin/awk '{print $1}'
}

sha256_remote() {
    # shellcheck disable=SC2029
    ssh_retry "$REMOTE_HOST" "[ -f $1 ] && sha256sum $1 2>/dev/null | awk '{print \$1}' || true"
}

# Post-sync smoke test for sync-omega.sh push.  Compares omega.db memory count
# and the hash of critical state files on both sides.  Returns 0 if everything
# matches, 1 if any mismatch is found.  Args: $1=local_home $2=remote_home
verify_sync_push() {
    local local_home="$1" remote_home="$2"
    local failures=0

    log "==> smoke test: verifying push"

    local lc rc
    lc=$(sqlite_count_local "$local_home/omega.db")
    rc=$(sqlite_count_remote "$remote_home/omega.db")
    if [ -z "$lc" ] || [ -z "$rc" ]; then
        warn "  memory count: could not read one or both DBs (local=$lc remote=$rc)"
        failures=$((failures + 1))
    elif [ "$lc" = "$rc" ]; then
        log "  memory count: $lc == $rc OK"
    else
        warn "  memory count: local=$lc remote=$rc MISMATCH"
        failures=$((failures + 1))
    fi

    # Hash check on a small set of critical files (license, profile, config, key).
    # llm_usage.db is intentionally NOT hashed — sqlite WAL leaves it slightly
    # non-deterministic across a checkpoint+rsync round-trip.
    local f lh rh
    for f in license.json profile.json config.json .key; do
        lh=$(sha256_local "$local_home/$f")
        if [ -z "$lh" ]; then continue; fi   # local doesn't have it, skip
        rh=$(sha256_remote "$remote_home/$f")
        if [ "$lh" = "$rh" ]; then
            log "  $f: hash matches OK"
        else
            warn "  $f: hash differs (local=${lh:0:12} remote=${rh:0:12}) MISMATCH"
            failures=$((failures + 1))
        fi
    done

    if [ "$failures" -eq 0 ]; then
        log "smoke test PASS"
        return 0
    fi
    warn "smoke test FAIL ($failures issue(s))"
    return 1
}

# Post-merge smoke test for merge-omega.sh.  Confirms both sides hold at least
# as many memories as the smaller pre-merge side (no rows lost) and reports
# any divergence.  Args: $1=local_before $2=remote_before $3=local_home $4=remote_home
verify_merge() {
    local lb="$1" rb="$2" local_home="$3" remote_home="$4"
    local failures=0

    log "==> smoke test: verifying merge"

    local la ra
    la=$(sqlite_count_local "$local_home/omega.db")
    ra=$(sqlite_count_remote "$remote_home/omega.db")
    if [ -z "$la" ] || [ -z "$ra" ]; then
        warn "  could not read DB counts after merge (local=$la remote=$ra)"
        return 1
    fi

    log "  local:  $lb -> $la (delta +$((la - lb)))"
    log "  remote: $rb -> $ra (delta +$((ra - rb)))"

    # Floor: neither side may have fewer rows than it started with.
    if [ "$la" -lt "$lb" ]; then
        warn "  local lost rows ($lb -> $la) FAIL"
        failures=$((failures + 1))
    fi
    if [ "$ra" -lt "$rb" ]; then
        warn "  remote lost rows ($rb -> $ra) FAIL"
        failures=$((failures + 1))
    fi

    # Union expectation: each side should now hold at least max(before).
    # Strict union may differ due to embedding-similarity dedup (>= 80%), so
    # treat shortfall as a soft warning rather than a hard failure.
    local expected_min=$lb
    [ "$rb" -gt "$lb" ] && expected_min=$rb
    if [ "$la" -lt "$expected_min" ] || [ "$ra" -lt "$expected_min" ]; then
        warn "  expected >= $expected_min on both sides (dedup absorbed novel rows?)"
    fi

    # Convergence: both sides should now have the same total.  Mismatch
    # can happen if one import failed silently or new rows were added
    # mid-merge; flag it but don't fail the script.
    if [ "$la" != "$ra" ]; then
        warn "  post-merge counts diverge (local=$la remote=$ra) — re-run with --consolidate if persistent"
    else
        log "  both sides converged to $la rows OK"
    fi

    if [ "$failures" -eq 0 ]; then
        log "smoke test PASS"
        return 0
    fi
    warn "smoke test FAIL ($failures issue(s))"
    return 1
}
