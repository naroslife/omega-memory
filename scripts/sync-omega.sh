#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/_omega_sync_lib.sh
source "$(dirname "$0")/_omega_sync_lib.sh"

# OMEGA full-state sync over SSH.
#
# Mirrors ~/.omega/ (memories, license, profile, configs, keys, documents,
# backups) and optionally the ~/.cache/omega/ model cache to a remote machine,
# so installing the omega-memory wheel there gives access to the same state.
#
# Usage:
#   scripts/sync-omega.sh push [--no-models] [--dry-run] [--yes]
#   scripts/sync-omega.sh pull [--no-models] [--dry-run]
#
# Configuration (environment variables):
#   OMEGA_REMOTE_HOST    (required)  SSH host, e.g. user@host
#   OMEGA_LOCAL_HOME     default: $HOME/.omega
#   OMEGA_REMOTE_HOME    default: $HOME/.omega on remote (literal, expanded by remote shell)
#   OMEGA_LOCAL_MODELS   default: $HOME/.cache/omega
#   OMEGA_REMOTE_MODELS  default: $HOME/.cache/omega on remote
#
# After push, on the remote:
#   pip install ./omega_memory-1.4.10+naroslife-py3-none-any.whl
#   omega setup     # registers MCP + Claude Code hooks using remote's paths
#   omega doctor

LOCAL_HOME="${OMEGA_LOCAL_HOME:-$HOME/.omega}"
REMOTE_HOME="${OMEGA_REMOTE_HOME:-\$HOME/.omega}"
LOCAL_MODELS="${OMEGA_LOCAL_MODELS:-$HOME/.cache/omega}"
REMOTE_MODELS="${OMEGA_REMOTE_MODELS:-\$HOME/.cache/omega}"
BACKUP_DIR="$HOME/.omega/backups"

# Exclusion patterns — host-specific or transient files.
# Applied to the ~/.omega/ tree; the model cache uses a separate (empty) exclude set.
EXCLUDES=(
    --exclude='*.sock'
    --exclude='*.pid'
    --exclude='mcp_pids/'
    --exclude='*.log'
    --exclude='logs/'
    --exclude='*.db-wal'
    --exclude='*.db-shm'
    --exclude='session-*.surfaced'
    --exclude='session-*.surfaced.json'
    --exclude='session-*.nudged'
    --exclude='session-reads/'
    --exclude='gates/'
    --exclude='stop_guard/'
    --exclude='post_edit_projects.json'
    --exclude='last-*'
)

usage() {
    cat <<EOF
Usage: $0 {push|pull} [--no-models] [--dry-run] [--yes]

  push    Local -> remote. Mirrors omega state + model cache to OMEGA_REMOTE_HOST.
  pull    Remote -> local. Saves snapshot under $BACKUP_DIR/omega-remote-<ts>/.

Flags:
  --no-models     Skip the embedding-model cache (~/.cache/omega).
  --dry-run       Pass -n to rsync; show what would change without writing.
  --yes           Skip the confirmation prompt on push.
  --allow-active  Proceed even if a remote MCP server is detected as running.
                  Pushes succeed but the remote will see a stale-inode window
                  on omega.db (~500 ms) until its connection refreshes.
  --no-verify     Skip the post-sync smoke test (default: verify omega.db
                  memory count and key file hashes match between sides).

The script auto-retries SSH/rsync up to 3 times with exponential backoff on
transient network failures.  Partial transfers resume on retry.
EOF
}

checkpoint_dbs() {
    # Sets a 30s busy_timeout so the checkpoint waits for live writers rather
    # than failing immediately on contention.  If the timeout still expires,
    # we log and continue — rsync will get a slightly-less-checkpointed DB,
    # which is still safe (WAL/SHM are excluded from the transfer).
    OMEGA_LOCAL_HOME="$LOCAL_HOME" python3 - <<'PY'
import sqlite3
import os

home = os.environ["OMEGA_LOCAL_HOME"]
for name in ("omega.db", "llm_usage.db"):
    path = os.path.join(home, name)
    if not os.path.exists(path):
        continue
    conn = sqlite3.connect(path, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            print(f"  checkpoint ok: {path}")
        except sqlite3.OperationalError as e:
            print(f"  checkpoint warn ({path}): {e} (continuing)")
    finally:
        conn.close()
PY
}

confirm() {
    local prompt="$1"
    if [ "${ASSUME_YES:-0}" = "1" ]; then
        return 0
    fi
    read -r -p "$prompt [y/N] " ans
    case "$ans" in
        y|Y|yes|YES) return 0 ;;
        *) echo "Aborted."; exit 1 ;;
    esac
}

# --- arg parsing -----------------------------------------------------------

[ $# -ge 1 ] || { usage; exit 1; }
case "$1" in
    -h|--help) usage; exit 0 ;;
esac
ACTION="$1"; shift
SYNC_MODELS=1
DRY_RUN=0
ASSUME_YES=0
ALLOW_ACTIVE=0
VERIFY=1
while [ $# -gt 0 ]; do
    case "$1" in
        --no-models)    SYNC_MODELS=0 ;;
        --dry-run)      DRY_RUN=1 ;;
        --yes)          ASSUME_YES=1 ;;
        --allow-active) ALLOW_ACTIVE=1 ;;
        --no-verify)    VERIFY=0 ;;
        -h|--help)      usage; exit 0 ;;
        *) echo "Unknown flag: $1"; usage; exit 1 ;;
    esac
    shift
done
export ALLOW_ACTIVE

RSYNC_OPTS=(-avz --human-readable --delete-excluded)
if [ "$DRY_RUN" = "1" ]; then
    RSYNC_OPTS+=(-n)
fi

require_remote() {
    if [ -z "${OMEGA_REMOTE_HOST:-}" ]; then
        echo "Error: set OMEGA_REMOTE_HOST (e.g. user@host)" >&2
        exit 1
    fi
    REMOTE_HOST="$OMEGA_REMOTE_HOST"
}

case "$ACTION" in
    push)
        require_remote
        if [ ! -d "$LOCAL_HOME" ]; then
            echo "Error: local omega home not found: $LOCAL_HOME" >&2
            exit 1
        fi

        echo "OMEGA push:"
        echo "  from: $LOCAL_HOME/"
        echo "  to:   $REMOTE_HOST:$REMOTE_HOME/"
        if [ "$SYNC_MODELS" = "1" ]; then
            echo "  models: $LOCAL_MODELS/ -> $REMOTE_HOST:$REMOTE_MODELS/"
        else
            echo "  models: skipped (--no-models)"
        fi
        [ "$DRY_RUN" = "1" ] && echo "  (dry-run)"
        confirm "Proceed?"

        log "preflight: checking remote MCP server state"
        preflight_remote_db "rsync push (would overwrite remote omega.db mid-write)" || exit 1

        log "==> WAL checkpoint"
        checkpoint_dbs

        log "==> rsync state (retry up to 3x with resumable --partial)"
        # shellcheck disable=SC2029  # remote path expansion is intentional
        ssh_retry "$REMOTE_HOST" "mkdir -p $REMOTE_HOME"
        rsync_retry "${RSYNC_OPTS[@]}" "${EXCLUDES[@]}" \
            "$LOCAL_HOME"/ "$REMOTE_HOST:$REMOTE_HOME/" \
            || die "rsync state failed after retries"

        if [ "$SYNC_MODELS" = "1" ] && [ -d "$LOCAL_MODELS" ]; then
            log "==> rsync model cache"
            # shellcheck disable=SC2029
            ssh_retry "$REMOTE_HOST" "mkdir -p $REMOTE_MODELS"
            rsync_retry "${RSYNC_OPTS[@]}" \
                "$LOCAL_MODELS"/ "$REMOTE_HOST:$REMOTE_MODELS/" \
                || die "rsync model cache failed after retries"
        fi

        # Smoke test: only meaningful for non-dry-run, since dry-run doesn't
        # actually write to the remote.
        if [ "$VERIFY" = "1" ] && [ "$DRY_RUN" != "1" ]; then
            verify_sync_push "$LOCAL_HOME" "$REMOTE_HOME" \
                || warn "smoke test reported issues; sync may need re-run"
        fi

        echo "Done."
        echo
        echo "Next steps on $REMOTE_HOST:"
        echo "  pip install <path-to>/omega_memory-1.4.10+naroslife-py3-none-any.whl"
        echo "  omega setup"
        echo "  omega doctor"
        ;;

    pull)
        require_remote
        mkdir -p "$BACKUP_DIR"
        TS=$(date +%Y%m%d-%H%M%S)
        STATE_DEST="$BACKUP_DIR/omega-remote-$TS"
        MODELS_DEST="$BACKUP_DIR/models-remote-$TS"

        echo "OMEGA pull:"
        echo "  from: $REMOTE_HOST:$REMOTE_HOME/"
        echo "  to:   $STATE_DEST/"
        if [ "$SYNC_MODELS" = "1" ]; then
            echo "  models: $REMOTE_HOST:$REMOTE_MODELS/ -> $MODELS_DEST/"
        else
            echo "  models: skipped (--no-models)"
        fi
        [ "$DRY_RUN" = "1" ] && echo "  (dry-run)"

        mkdir -p "$STATE_DEST"
        log "==> rsync state (retry up to 3x with resumable --partial)"
        rsync_retry "${RSYNC_OPTS[@]}" "${EXCLUDES[@]}" \
            "$REMOTE_HOST:$REMOTE_HOME/" "$STATE_DEST/" \
            || die "rsync state failed after retries"

        if [ "$SYNC_MODELS" = "1" ]; then
            mkdir -p "$MODELS_DEST"
            log "==> rsync model cache"
            rsync_retry "${RSYNC_OPTS[@]}" \
                "$REMOTE_HOST:$REMOTE_MODELS/" "$MODELS_DEST/" \
                || die "rsync model cache failed after retries"
        fi

        echo "Done."
        echo "Snapshot at: $STATE_DEST"
        ;;

    *)
        usage
        exit 1
        ;;
esac
