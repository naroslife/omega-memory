# Learning / Memory Systems for Claude Code — Decision Comparison

_Compiled 2026-05-27. Audience: solo dev running several concurrent Claude Code sessions on `omega-memory`._

## TL;DR

These four tools solve **three different problems** and only partially overlap. **OMEGA** (this repo) is the winner for the core need — durable, semantically-recalled cross-session memory plus multi-agent coordination, which is exactly the "several sessions at once" pain. **claude-mem** is the strongest runner-up and the more battle-tested general-purpose memory tool (vastly larger user base), but it overlaps OMEGA's job and is less suited to multi-agent coordination. **Caliber** (config sync) and **ECC continuous-learning** (instinct distillation) do *different* jobs and can coexist with the winner — but running all three memory/learning hook stacks at once is redundant and stacks hook overhead, so pick one memory layer.

## Comparison Table

| Dimension | OMEGA | claude-mem | ECC continuous-learning v2 | Caliber |
|---|---|---|---|---|
| **Primary purpose** | Cross-session memory + multi-agent coordination | Cross-session memory (context recall) | Pattern/"instinct" distillation → skills/agents | Agent-config sync across tools + learnings file |
| **What it captures** | Decisions, lessons, error patterns, prefs (typed) | Full session activity, AI-compressed into typed observations (bugfix/discovery/decision) | Atomic learned behaviors w/ confidence (0.3–0.9), project- or global-scoped | Codebase-tailored config + extracted session "learnings" |
| **Storage** | SQLite + sqlite-vec (384-dim vector) + FTS5 lexical; ONNX embeddings + reranker; `~/.omega/` | SQLite (`~/.claude-mem/`) + Chroma vector DB; AI-compressed summaries | JSONL observations + YAML instinct files under `~/.claude/homunculus/` (markdown skills when evolved) | `CALIBER_LEARNINGS.md` + CLAUDE.md / Cursor / Copilot / Codex config files (markdown) |
| **Retrieval into context** | Auto-surfaced at session start via hook + on-demand MCP `omega_query`; vector+lexical+rerank | 3-layer MCP workflow: `search` index → `timeline` → `get_observations` (token-frugal) | Instincts injected as learned behaviors; `/evolve` clusters into skills | Configs loaded as normal CLAUDE.md / rules at session start |
| **Integration model** | MCP server + hook **daemon** (UDS socket); or hooks-only mode | 5 lifecycle hooks + 4 MCP tools + **worker service on :37777** (Bun) + web viewer | PreToolUse/PostToolUse hooks → **background observer agent (Haiku)** every ~5 min | pre-commit hook + PostToolUse / UserPromptSubmit / SessionEnd hooks; `caliber refresh` CLI |
| **Runtime overhead** | Daemon process; embedding model in RAM (~hundreds MB; hooks-only saves ~600MB per docs). Hook latency designed fail-open. **[ASSUMPTION]** exact RAM varies | Persistent HTTP worker (:37777) + Bun + uv runtimes; no published metrics **[ASSUMPTION]** | Periodic background Haiku agent = recurring API calls/cost + per-tool-call hook write | Light: hook writes + on-commit LLM call for refresh; no resident daemon |
| **Setup complexity** | `pip install`, `omega setup`, `omega doctor` (downloads ~90MB model) | `npx claude-mem install` (auto-pulls Bun/uv) | Plugin install + enable observer in config | `/setup-caliber`, installs pre-commit hook |
| **Local-first / privacy** | Yes — fully local, no cloud, no API keys (core) | Yes — local SQLite/Chroma, no cloud | Yes — observations stay local; only instincts exportable | Local files; **refresh uses an LLM provider** (your key) to tailor configs |
| **Maturity / evidence** | ~147★ (`omega-memory/omega-memory`), Apache-2.0, ~1100+ tests in repo, active. Small community | **~78.6k★, 6.8k forks, v13.3.0, 270+ releases** — by far the most adopted. Apache-2.0 | Part of ECC (~170k★ umbrella plugin); huge reach but stars are for the whole collection, not this subsystem | ~1.1k★, MIT, active (last push May 2026). Newer/smaller |
| **Benchmarks** | None published found | None published found (vendors claim "~10x token savings," "80% signal rate" — unverified) | None | None |

_No independent third-party benchmarks were found for any of the four. Star counts are popularity proxies, not quality/benchmark evidence._

## One-Paragraph Summaries

**OMEGA** — The repo you're in. A local-first MCP server + hook daemon that stores typed memories (decisions/lessons/errors/preferences) in SQLite with sqlite-vec vector search, FTS5 lexical search, and an ONNX reranker, then auto-surfaces relevant memories at session start. Its differentiator vs the others is **multi-agent coordination** (file/branch locking, intent broadcasting, agent messaging, task queues — Pro tier), which directly addresses running several concurrent sessions. Smaller community than claude-mem but strong test coverage and the only one purpose-built for the multi-session case.

**claude-mem** — The most popular tool here by a wide margin (~78.6k★). Captures the *entire* session, AI-compresses transcripts into short typed observations, stores them in SQLite + Chroma, and recalls via a deliberately token-cheap 3-layer search workflow. Runs a persistent worker service (port 37777, Bun runtime) with a web viewer. Excellent general-purpose "Claude forgets everything" fix, but it's single-agent-centric and has no coordination layer; it overlaps OMEGA's memory role almost entirely.

**ECC continuous-learning v2** — Not really a "memory" system; it's a **pattern distiller**. Hooks log every tool call; a background Haiku agent mines repeated workflows, user corrections, and error fixes into atomic "instincts" with confidence scores, project- or globally-scoped, that can `/evolve` into skills/commands/agents. Complementary in *purpose* to OMEGA/claude-mem (learns *how you work* vs *what you decided*), but it stacks its own PreToolUse/PostToolUse hooks and incurs recurring background-agent cost.

**Caliber** — Solves a **different problem entirely**: keeping agent configs (CLAUDE.md, Cursor, Copilot, Codex) in sync and tailored to the codebase, plus extracting session "learnings" into `CALIBER_LEARNINGS.md`. Runs via pre-commit + lifecycle hooks and an LLM-backed `caliber refresh`. It is config-management with a learnings sidecar, not a recall engine — least redundant with the others.

## Recommendation

- **Winner: OMEGA** — for this user's stated use case (durable cross-session memory **plus** several simultaneous sessions). The coordination layer (file/branch locking, agent messaging) is unique among the four and is the deciding factor; semantic recall covers the base memory need. Bonus: it's the repo being dogfooded, so improvements compound.
- **Runner-up: claude-mem** — if multi-agent coordination weren't needed, its maturity (78.6k★, 270+ releases) and token-frugal recall would make it the safer general pick. Choose it over OMEGA only if you value proven adoption over coordination + local-stack control, or if OMEGA's recall quality disappoints in practice.
- **Do NOT run OMEGA and claude-mem together** — they do the same job, double the hook/daemon overhead, and would surface two competing memory contexts. Pick one memory layer.
- **Caliber: keep, complementary** — different job (config sync). Safe to run alongside the chosen memory tool. Its hooks are the lightest of the set.
- **ECC continuous-learning: optional, evaluate cost** — complementary in purpose (instinct distillation) but it's the heaviest to justify: extra hooks on every tool call **plus** a recurring background Haiku agent. The user already observed ECC + OMEGA + Caliber overlap and stack hook events. Recommendation: **disable the ECC observer** while running OMEGA unless instinct→skill evolution is actively used; re-enable selectively. If kept, accept the recurring API cost.
- **Net suggested stack:** OMEGA (memory + coordination) + Caliber (config sync). Drop claude-mem (redundant with OMEGA). Treat ECC continuous-learning as opt-in for skill mining only.

## Assumptions & Gaps

- **[ASSUMPTION]** OMEGA's exact resident RAM isn't precisely benchmarked here; README cites "~600MB RAM saved" in hooks-only mode and a ~90MB model download, so full mode is meaningfully higher, but no single authoritative figure was found. Project memory notes reference a "4 GB RSS limit" guard, implying real-world footprint can be large.
- **[ASSUMPTION]** claude-mem's runtime overhead (Bun + uv + persistent :37777 worker) has **no published metrics**; "continuous HTTP worker" overhead is inferred from its architecture, not measured.
- **[ASSUMPTION]** claude-mem's "~10x token savings" and ">80% signal rate" are **vendor/blog claims**, not independently verified benchmarks.
- **GAP:** `github.com/omega-memory/omega` (the URL in the task) redirects/resolves to **`omega-memory/omega-memory`** (~147★, created 2026-02-13, Apache-2.0, Python). Star figure is for that canonical repo; this local fork is `naroslife` with a private `omega-platform` submodule whose Pro features (coordination, routing) couldn't be independently star-counted.
- **GAP:** ECC stars (~170k) are for the **entire everything-claude-code plugin collection** (`affaan-m/everything-claude-code`, 48 agents / 184 skills), **not** the continuous-learning-v2 subsystem specifically. No standalone adoption metric exists for the instinct system. Local install is plugin v1.9.0; the public repo may be newer.
- **GAP:** No independent third-party **benchmarks** (recall accuracy, latency, context-budget impact) were found for **any** of the four. All comparisons on recall quality are architectural inference, not measured.
- **[ASSUMPTION]** ECC continuous-learning's cost characterization ("recurring API calls") assumes the background observer agent runs against a billed model (Haiku per the SKILL.md); if pointed at a local model the cost claim doesn't hold.
- **[ASSUMPTION]** Caliber's `refresh` is described as using "an LLM provider"; the privacy caveat (your prompt/config sent to that provider) is inferred from its interactive provider-setup behavior, not from a published data-flow doc.
- **GAP:** Caliber's npm/distribution package name wasn't confirmed (the obvious `caliber`/`@caliber-ai/caliber` names didn't match); install is via the project's `/setup-caliber` flow rather than a verified registry package.

---
_Sources: project READMEs/SKILL.md (local), GitHub repo metadata via `gh`, claude-mem GitHub + tutorial/review articles (DataCamp, andrew.ooo, YUV.AI, Medium), Augment Code ECC star-count writeups._
