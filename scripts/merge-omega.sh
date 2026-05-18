#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/_omega_sync_lib.sh
source "$(dirname "$0")/_omega_sync_lib.sh"

# OMEGA two-way memory merge over SSH.
#
# Both sides export their memory DB to JSON, swap files, then non-destructively
# import — relying on omega's content-hash + embedding dedup pipeline to avoid
# duplicates. End state: both DBs hold the union of memories.
#
# Usage:
#   scripts/merge-omega.sh                          # full merge, both sides
#   scripts/merge-omega.sh --dry-run                # export + swap only, skip imports
#   scripts/merge-omega.sh --consolidate            # merge + run `omega consolidate` after
#   scripts/merge-omega.sh --allow-active           # proceed even with active MCP servers
#   scripts/merge-omega.sh --no-verify              # skip post-merge smoke test
#
# Configuration (environment variables):
#   OMEGA_REMOTE_HOST  (required)  SSH host, e.g. user@host
#   OMEGA_REMOTE_BIN   default: omega   (path to omega CLI on the remote)
#   OMEGA_LOCAL_BIN    default: omega   (path to omega CLI locally)
#
# Resilience:
#   - SSH and scp auto-retry 3x with 2/4/8 s backoff on transient failures.
#   - `omega import --merge` auto-retries 3x with 5/10/20 s backoff if the DB
#     is locked by another writer (only relevant under --allow-active).
#   - On SIGINT/SIGTERM the staging files are cleaned up on BOTH sides.
#
# Note: complements scripts/sync-omega.sh.  sync-omega is a one-way mirror;
# merge-omega is a two-way merge that preserves novel memories on both sides.

DRY_RUN=0
WITH_CONSOLIDATE="${WITH_CONSOLIDATE:-0}"
ALLOW_ACTIVE=0
VERIFY=1
for arg in "$@"; do
    case "$arg" in
        --dry-run)      DRY_RUN=1 ;;
        --consolidate)  WITH_CONSOLIDATE=1 ;;
        --allow-active) ALLOW_ACTIVE=1 ;;
        --no-verify)    VERIFY=0 ;;
        -h|--help)
            /usr/bin/sed -n '7,31p' "$0" | /usr/bin/sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "Unknown flag: $arg" >&2; exit 1 ;;
    esac
done
export ALLOW_ACTIVE

REMOTE_HOST="${OMEGA_REMOTE_HOST:?Set OMEGA_REMOTE_HOST (e.g. user@host)}"
REMOTE_BIN="${OMEGA_REMOTE_BIN:-omega}"
LOCAL_BIN="${OMEGA_LOCAL_BIN:-omega}"

WORK=$(mktemp -d -t omega-merge-XXXXXX)
chmod 700 "$WORK"

# Remote staging paths.  $HOME stays unquoted in transit and is expanded by
# the *remote* shell when ssh runs.  REMOTE_BIN/REMOTE_STAGE/REMOTE_INBOX, by
# contrast, are deliberately expanded client-side (they're local config).
# shellcheck disable=SC2016
REMOTE_STAGE='$HOME/.omega/.merge-staging.json'
# shellcheck disable=SC2016
REMOTE_INBOX='$HOME/.omega/.merge-inbox.json'

# Cleanup runs on EXIT, SIGINT, SIGTERM — clears local tempdir and best-effort
# removes the remote staging files even if the script was interrupted.
cleanup() {
    /usr/bin/rm -rf "$WORK"
    if [ -n "${REMOTE_HOST:-}" ]; then
        # shellcheck disable=SC2029
        ssh -o ConnectTimeout=5 "$REMOTE_HOST" \
            "/bin/rm -f $REMOTE_STAGE $REMOTE_INBOX" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

log "preflight: checking local + remote MCP server state"
preflight_local_db  "omega import --merge" || die "local preflight failed (use --allow-active to override)"
preflight_remote_db "omega import --merge" || die "remote preflight failed (use --allow-active to override)"

# Snapshot pre-merge memory counts so the post-merge smoke test can report
# the row delta and detect data loss.
LOCAL_HOME_RUNTIME="${OMEGA_LOCAL_HOME:-$HOME/.omega}"
# shellcheck disable=SC2016  # $HOME is expanded by the remote shell via ssh
REMOTE_HOME_RUNTIME='$HOME/.omega'
LOCAL_COUNT_BEFORE=$(sqlite_count_local "$LOCAL_HOME_RUNTIME/omega.db" || echo "")
REMOTE_COUNT_BEFORE=$(sqlite_count_remote "$REMOTE_HOME_RUNTIME/omega.db" || echo "")
log "pre-merge counts: local=${LOCAL_COUNT_BEFORE:-?} remote=${REMOTE_COUNT_BEFORE:-?}"

log "==> export local"
retry_on_db_lock "$LOCAL_BIN" export "$WORK/local.json"

log "==> export remote (over ssh)"
# shellcheck disable=SC2029  # REMOTE_BIN/STAGE intentionally expand client-side
ssh_retry "$REMOTE_HOST" "$REMOTE_BIN export $REMOTE_STAGE" \
    || die "remote export failed"

log "==> swap JSON via scp (both directions)"
scp_retry -q "$REMOTE_HOST:$REMOTE_STAGE" "$WORK/remote.json" \
    || die "scp remote -> local failed"
scp_retry -q "$WORK/local.json" "$REMOTE_HOST:$REMOTE_INBOX" \
    || die "scp local -> remote failed"

if [ "$DRY_RUN" = "1" ]; then
    log "(dry-run) would import:"
    log "    local  <- $WORK/remote.json (--merge)"
    log "    remote <- $REMOTE_INBOX (--merge)"
    log "exiting before imports"
    exit 0
fi

log "==> import remote -> local (--merge, retry-on-db-lock)"
retry_on_db_lock "$LOCAL_BIN" import "$WORK/remote.json" --merge \
    || die "local import failed"

log "==> import local -> remote (--merge, retry-on-db-lock)"
# shellcheck disable=SC2029
ssh_retry "$REMOTE_HOST" "$REMOTE_BIN import $REMOTE_INBOX --merge" \
    || die "remote import failed"

if [ "$WITH_CONSOLIDATE" = "1" ]; then
    log "==> consolidate (fold fuzzy near-duplicates)"
    "$LOCAL_BIN" consolidate || warn "local consolidate failed (non-fatal)"
    # shellcheck disable=SC2029
    ssh_retry "$REMOTE_HOST" "$REMOTE_BIN consolidate" \
        || warn "remote consolidate failed (non-fatal)"
fi

if [ "$VERIFY" = "1" ] && [ -n "$LOCAL_COUNT_BEFORE" ] && [ -n "$REMOTE_COUNT_BEFORE" ]; then
    verify_merge "$LOCAL_COUNT_BEFORE" "$REMOTE_COUNT_BEFORE" \
        "$LOCAL_HOME_RUNTIME" "$REMOTE_HOME_RUNTIME" \
        || warn "smoke test reported issues; inspect the counts above"
fi

log "Merge complete. Both DBs now hold the union of memories."
