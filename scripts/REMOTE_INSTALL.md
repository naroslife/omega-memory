# Installing & Syncing omega-memory to a remote machine

Companion guide for `sync-omega.sh` and `merge-omega.sh`. Assumes you have
already built the wheel locally (`python -m build --wheel`) and uploaded
`dist/omega_memory-<version>-py3-none-any.whl` to `~/dev/` on the remote
via `scp` (or any transport you prefer).

The remote is referenced below as `$REMOTE` — the SSH host alias from your
`~/.ssh/config`.

---

## 1. Install the wheel on the remote

```bash
ssh "$REMOTE"
python3 -m venv ~/dev/omega-venv
~/dev/omega-venv/bin/pip install --upgrade ~/dev/omega_memory-1.4.10+naroslife-py3-none-any.whl
# Put it on PATH for the remaining steps:
ln -sf ~/dev/omega-venv/bin/omega ~/.local/bin/omega
# or: export PATH="$HOME/dev/omega-venv/bin:$PATH"
omega --version   # expect 1.4.10+naroslife
exit
```

## 2. Initial PUSH — mirror ~/.omega/ to the remote

Delivers the memory DB, license, profile, configs, key, and the embedding
model cache. Runs a smoke test verifying memory-count and key-file hashes
match between sides.

```bash
export OMEGA_REMOTE_HOST="$REMOTE"
cd ~/dev/omega-memory
./scripts/sync-omega.sh push --yes
```

Flags worth knowing:
- `--no-models` — skip the ~127 MB embedding model cache (use if the remote
  already has it or the link is slow).
- `--dry-run` — preview the rsync changes without writing.
- `--allow-active` — proceed even if a remote MCP server is detected as
  running (you'll see a brief stale-inode window on `omega.db`).
- `--no-verify` — skip the post-sync smoke test.

## 3. Re-register MCP + hooks on the remote

**Required.** The synced `settings.json` / `claude_desktop_config.json`
contain absolute paths from the source machine that won't resolve on the
remote.

```bash
ssh "$REMOTE"
omega setup --client claude-code   # or claude-desktop / cursor / windsurf / etc.
omega doctor                       # verify install + state
exit
```

`omega doctor` exits non-zero on hard failures and runs five bounded-latency
probes (embedding semantic similarity, reranker ranking, embed daemon,
hook server, MCP server). Pass `--strict` to escalate warnings to failures.

---

## Ongoing sync

### Two-way merge (preserves novel memories on both sides)

```bash
export OMEGA_REMOTE_HOST="$REMOTE"
./scripts/merge-omega.sh
```

Both sides export their memories as JSON, swap files, and import with
`--merge`. The 3-stage dedup pipeline (canonical hash → content hash →
embedding cosine ≥ 0.80) absorbs duplicates. Requires `omega` on PATH on
both sides (the symlink from step 1 handles the remote).

Flags:
- `--dry-run` — export + swap only, skip the imports.
- `--consolidate` — run `omega consolidate` afterward to fold near-dupes.
- `--allow-active` — proceed with a live MCP server (retries on DB lock).
- `--no-verify` — skip the post-merge smoke test.

### One-way refresh

```bash
./scripts/sync-omega.sh push --yes   # local -> remote (overwrites remote)
./scripts/sync-omega.sh pull         # remote -> ~/.omega/backups/omega-remote-<ts>/
```

---

## Resilience notes

Both scripts source `scripts/_omega_sync_lib.sh`, which provides:
- SSH / scp / rsync auto-retry (3 attempts, 2/4/8 s backoff).
- Resumable rsync via `--partial --partial-dir=.rsync-partial`.
- DB-lock retry (3 attempts, 5/10/20 s backoff) wrapping `omega import`.
- Pre-flight detection of live MCP servers on either side via the
  `~/.omega/mcp_pids/` registry — refuses to run unless `--allow-active`.
- Cleanup trap on EXIT/INT/TERM clearing remote staging files.
- Post-operation smoke test (memory count + sha256 of license/profile/
  config/.key; `llm_usage.db` skipped due to WAL non-determinism).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `omega: command not found` on remote | symlink in step 1 missing | Re-create it or add the venv to PATH |
| `omega export` rejects on remote | stale `omega` on PATH | Point `OMEGA_REMOTE_BIN` at the venv binary: `OMEGA_REMOTE_BIN=~/dev/omega-venv/bin/omega ./scripts/merge-omega.sh` |
| `database is locked` during merge | live MCP server holds write lock | Close Claude sessions, or pass `--allow-active` (script auto-retries on DB lock) |
| Smoke test fails on `omega.db` count | sync interrupted mid-write | Re-run with same flags; `--partial` resumes |
| Smoke test fails on file hash | host-specific file leaked through excludes | Check what differs: `ssh "$REMOTE" sha256sum ~/.omega/<file>` vs local |
