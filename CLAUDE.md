# OMEGA — Project Instructions

**naroslife fork** of [omega-memory/omega](https://github.com/omega-memory/omega). Pro features live in private submodule `src/omega_platform/`.

## Tech Stack

- **Python** 3.11+ (CI: 3.11 / 3.12 / 3.13) · **Build**: `hatchling` · packages `src/omega`, `src/omega_platform`
- **Storage**: SQLite + sqlite-vec (vector, 384-dim) + FTS5 (lexical)
- **Embeddings**: ONNX model cached locally · reranker int8 default
- **Server**: MCP · `starlette`/`uvicorn` for HTTP
- **CLI**: `omega = omega.cli:main`
- **Deps**: `onnxruntime`, `tokenizers`, `sqlite-vec`, `numpy`, `orjson`, `rich`, `markdownify`, `cryptography`

## Project Map

**Public** package (`src/omega/`) covers the bridge API, storage, retrieval, MCP server, hooks, and core types.

**Pro** (`src/omega_platform/`, submodule) provides cloud, oracle, orchestrator, router, knowledge, ingest, entity, profile, daemon hook server, embedding daemon, license, protocol modules.

**Standalone hooks** mirrored at `hooks/` (repo-root) dispatch through the daemon UDS socket.

**Tests** (`tests/`): ~1500 across 100+ files — including `tests/server/`, `tests/test_hooks/`. Shared fixtures in `tests/conftest.py`.

**Scripts** (`scripts/`): release, sync, clean store, benchmarks, latency profiling, etc.

**Benchmarks**: `benchmarks/memorystress/`.

**Other**: `installer/` (macOS pkg, Windows iss), `skills/omega-memory/SKILL.md`, `openclaw-skill/SKILL.md`, `integrations/`, `docs/`.

**Repo metadata directories**:
- `.github/` — issue templates, GitHub Actions workflows, `FUNDING.yml`, `PULL_REQUEST_TEMPLATE.md`
- `.mypy_cache/` — mypy type-check cache (contains `CACHEDIR.TAG`)
- `.ruff_cache/` — ruff lint cache (contains `CACHEDIR.TAG`)
- `.well-known/` — `mcp` discovery endpoint metadata
- `.worktrees/` — git worktree state for parallel branch work
- `assets/` — demo recording assets (`demo.gif`, `demo.sh`)
- `translations/` — translated READMEs (`README_de.md`, `README_es.md`)

## Build & Verify

```bash
pip install -e ".[dev,server]"
omega setup
omega doctor
```

## Test & Lint

```bash
pytest tests/
pytest tests/ --cov=omega
pytest tests/test_bridge_helpers.py -v
pytest tests/ -m slow
ruff check src/ tests/
```

## Submodule Operations

```bash
git submodule update --init --recursive
git -C src/omega_platform status
git add src/omega_platform
```

- `asyncio_mode = auto` (see `pyproject.toml`) — no `@pytest.mark.asyncio` needed
- Default `addopts = -m 'not slow'` excludes `slow` marker · platform markers: `windows`, `unix`
- Lint ignores (pre-existing, don't reintroduce others): `E402, E721, E741, F841, F821`
- line-length=120, target py311

## Conventions

- **Commits**: Conventional Commits — `feat:`, `fix:`, `chore:`, `refactor:`, `docs:`, `test:`, `ci:`, `perf:`
- **Versioning**: keep `pyproject.toml:version` + `src/omega/__init__.py:__version__` in sync
- **Imports**: lazy imports are intentional. Optional dep groups guard Pro imports — don't promote Pro-only deps into core

## Fork Workflow

- Push fork work to the fork remote. Never push to `main` (tracks upstream)
- Upstream syncs handled by scripts in `scripts/`
- `src/omega_platform/` is a submodule — bumping requires a separate `chore(submodule): …` commit referencing the new SHA

## Notes

- This project ships its own MCP server (`omega-memory`); global `omega_*` tools are this codebase — memory changes are dogfooded
- Prefer Serena for symbol-level edits in `src/omega/`. `src/omega_platform/` is a separate git repo — commits must happen inside the submodule directory
- When changing hook code, restart the hook daemon (or full Claude Code session)
- See @./CONTRIBUTING.md for upstream contribution rules

## Model Configuration

Recommended default: `claude-sonnet-4-6` with high effort (stronger reasoning; higher cost and latency than smaller models).
Smaller/faster models trade quality for speed and cost — pick what fits the task.
Pin your choice (`/model` in Claude Code) so upstream default changes do not silently change behavior.
