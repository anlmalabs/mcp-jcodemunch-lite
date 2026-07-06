# animamunchmcp — SOP

> **Maintenance note:** This file is the binding SOP for agentic work in this repo. When jcm releases a new tool (tracked in `CLAUDE.md` release notes), the matching section below must be updated.
> **Alerting:** GitHub-native error-spike alert rule lives in `.github/workflows/error-spike.yml` and `.github/actions/error-spike-to-issue/`; production error spikes turn into auto-created GitHub issues via `gh issue create`. See `docs/OBSERVABILITY.md` §1.1.

## 0. Absolute token-efficiency commitment

The agent MUST prefer a **structured MCP call** (`jcm`) for every code operation. Native `Read` / `Grep` / `Glob` / `Bash` are reserved for:

- Precondition before a native `Edit` / `Write` (harness only accepts edits on files previously read via `Read`).
- Latency-native checks where no MCP route exists (rare).

**Default routing for reads and writes:**
- Symbol lookup, navigation, impact analysis, repo-wide search → **jcm**.
- Listing files, directory scans, glob discovery → **jcm** `get_file_tree`.

## 1. Tool routing matrix

Use the FIRST column that fits the goal. Only fall back to native fallback if jcm route is unavailable.

| User goal | Prefer jcm (repo-wide semantic) | Native fallback |
|---|---|---|
| Cold-start orientation | `get_repo_map`, `get_tectonic_map`, `get_repo_health` | — |
| "Find the symbol/function X" | `search_symbols(semantic=true or fusion=true)` | — |
| "What does file X look like" | `get_file_outline` → `get_context_bundle` | `Read` (pre-Edit only) |
| "Who calls / uses X" (cross-file) | `find_references`, `get_blast_radius`, `get_call_hierarchy` | — |
| "Implementations of abstract X" | `find_implementations(repo=..., symbol=...)` | — |
| "Where is X defined, from a usage site" | `get_context_bundle(symbol_ids=[id])` | — |
| "Scan for pattern / rule / anti-pattern in code" | `search_ast(category=security)` etc. | `Grep` (fallback) |
| "Find string literal / comment / doc" | `search_text(query, context_lines=3)` | — |
| "List files / tree / glob" | `get_file_tree(path_prefix=...)` | `Glob` (fallback) |
| "Read a file I'm going to Edit" | `get_file_content` | `Read` (pre-Edit precondition) |
| Create / overwrite a file | — | `Write` |
| "Regex-driven substitution in file" | `plan_refactoring(refactor_type=signature)` → edit plan | `Edit` (preferred once located) |
| Safe symbol delete / rename | `check_safe(mode=edit|delete)` THEN Edit | `Edit` |
| Diagnose type errors | — | — |
| Diff / PR risk | `get_changed_symbols`, `get_pr_risk_profile` | — |

## 2. Opening playbook (cold start + per-request)

### 2.1 Cold-start / repo never seen
```
# 1. confirm index (jcm)
resolve_repo(path=".")
   # if not indexed: index_folder(path=".")
# 2. one-call intent capsule (jcm)
assemble_task_context(repo="animaios/animamunchmcp", task=<user message>, model=<active model>)
   # honor _meta.confidence: high=proceed / medium=narrow / low=report gap, stop
# 3. optionally expand first-contact files
get_file_outline
```

### 2.2 Per-request dispatch
- **Explore / read:** jcm `search_symbols(fusion=true)` → `get_context_bundle(symbol_ids=[...])`.
- **Edit / refactor:** jcm `plan_refactoring` OR `check_safe(mode=edit)` THEN native `Edit`. Use jcm for cross-file blast-radius.
- **Pattern / security scan:** jcm `search_ast(category=security)`.
- **Impact / who-uses:** jcm `get_blast_radius(depth=2, include_source=true)` + `find_references`.
- **Definition site:** jcm `get_context_bundle(symbol_ids=[id])`.
- **Diagnostics / lint:** native tooling.

### 2.3 Post-edit
```
register_edit(repo="animaios/animamunchmcp", file_paths=[<edited>], reindex=true)

# optional re-check for blast-radius-sensitive edits:
get_blast_radius(symbol=<touched>, depth=2, include_source=true)
get_pr_risk_profile(base_ref="main", head_ref="HEAD")
```

## 3. jcm MCP cheatsheet (repo-wide semantic)

### Cold-start
- `resolve_repo`, `assemble_task_context` — **ALWAYS the first two calls**. Task capsule = `assemble_task_context(repo="animaios/animamunchmcp", task=...)`.
- `get_repo_map`, `get_tectonic_map`, `get_repo_health`, `get_project_intel` — repo topology, module boundaries, dead-code %, Dockerfiles/CI/deps discovery.

### Read (structured — never full files by default)
- `get_file_outline` → `get_symbol_source` (single ID → flat, array → batch) → `get_context_bundle(symbol_ids=[...], budget_strategy="core_first")`.
- `get_file_content` (last resort, line range), `get_file_tree(path_prefix=)`.

### Search
- `search_symbols` — `mode="context"` (ranked, query-less), `mode="winnow"` (multi-axis), `semantic=true`, `fusion=true` (Weighted Reciprocal Rank), `detail_level="compact"` + `token_budget=3000` for broad discovery.
- `search_text` (full text), `search_ast(category="security"|"all")`, `search_columns` (dbt/SQLMesh).

### Graph / impact
- `find_references` (`mode="refs"|"importers"|"related"`, `quick=true` for dead-code, `cross_repo=true`).
- `get_call_hierarchy(symbol_id=..., direction="both", depth=3)` (+ `chains=true` + `kind="http"|"cli"|` for route discovery).
- `get_blast_radius(symbol=..., include_source=true, call_depth=1, include_decisions=true)`.
- `check_safe(mode="edit"|"delete")`, `plan_refactoring(refactor_type="rename"|"move"|"extract"|"signature")`.

### Bulk & triage
- `find_similar_symbols`, `find_implementations`, `get_class_hierarchy`, `get_symbol_provenance`, `get_symbol_complexity`, `get_dead_code_v2(min_confidence=0.67)`, `find_hot_paths(top_n=20)`.
- `get_changed_symbols(include_blast_radius=true, max_blast_depth=3)`, `get_pr_risk_profile(base_ref="main", head_ref="HEAD")`.

### Index lifecycle
- `index_folder` / `index_file` / `index_repo` / `invalidate_cache`, `embed_repo(force=true, batch_size=50)`, `summarize_repo(force=true)`, `register_edit(file_paths=[...])`.

### Feature flags
- New feature flags are registered as `FF.NAME = _Flag("name", ...)` in `src/jcodemunch_mcp/feature_flags.py` (single source of truth for both the live registry and the CI); `quality-gates.yml` `dead-feature-flags-check` fails PRs that leave a flag registered but unreferenced in `src/`.

## 4. Standard Operating Procedure — Local Helper Scripts

The following scripts are installed at `~/.agents/skills/`. Use them when the
situation calls for it. Soft guidance — apply judgment, not dogma.

### auto-execute (plan runner)

`~/.agents/skills/auto-execute/auto-execute.sh`

Use when the user has an implementation plan (Markdown with `### Task N:` headers)
and wants it executed task-by-task in isolated sessions.

CLI binaries: any `claude`-prefixed command name. Pass 1-5 for failover redundancy.

Usage:
```
auto-execute.sh [--max-turns 80] [--task-timeout 600] [--context-limit 100000]
                [--start-task N] [--log-dir logs/auto-execute]
                <claude-binary> [claude-binary-2..5] <plan-input>
```

Plan input: path to plan file, partial match against `docs/plans/`, or `latest` for
most recent.

Behavior: runs each task as `claude -p "/auto-execute task-N.md"`, monitors for
timeout/context-limit, verifies exit+commit+tree-clean after each step, retries
with next binary on failure. Ctrl+C is trapped cleanly.

Do not use for single-shot tasks, exploratory work, or when the user wants to
review each step manually.

### commit (git committer)

`~/.agents/skills/commit/SKILL.md` — Claude Code skill, triggered via `/commit`

Use when the user says "commit" or "save this" or asks for a commit message.
Generates `type: description` format, runs `git diff --staged`, prompts for staging
if nothing staged.

Alternative without skill invocation:
```
git diff --staged → git add . → git commit -m "type: description"
```

### humanizer (writer)

`~/.agents/skills/humanizer/SKILL.md` — Claude Code skill, triggered via
`/humanizer`

Use when the user asks to "clean up" writing, "make this sound natural", "de-AI
this", or similar. Applies 25 pattern replacements (significance inflation, copula
avoidance, chatbot artifacts, em dash overuse, etc.) with personality-injection
pass. Repo-agnostic — works on any prose.

### When to invoke automatically

| User says | Invoke |
|-----------|--------|
| "run the plan" / "execute tasks from <file>" | auto-execute |
| "commit this" / "commit changes" | commit skill or manual |
| "fix this text" / "make it sound human" / "de-AI" | humanizer skill |

Platform check: these scripts assume Linux/macOS. On Windows, use PowerShell
equivalents (not yet ported).

## 5. Hard rules (MUST)

1. The SOP is a **soft default**: prefer jcm first; native `Read` / `Grep` / `Glob` / `Bash` allowed when required as harness precondition for Edit, or when no comparable MCP route exists.
2. NEVER read full files via native `Read` unless Edit or Write on that file is imminent. Prefer `get_context_bundle`, `get_symbol_source`.
3. Before ANY edit / delete / rename: run jcm `check_safe(mode=edit|delete)` and Re-run after the change.
4. After `search_symbols` returns `_meta.verdict="no_implementation_found"`: DO NOT re-search with different terms and DO NOT assume a related file implements the feature. Report the gap to the user. Only use `related_existing` as "nearby" hint.
5. Honor `_meta.confidence` (0-1): ≥ 0.8 → trust top result; ≤ 0.4 → widen search or report gap, DO NOT proceed as-is.
6. Edits with blast radius > same-file → jcm `plan_refactoring` first, then native Edit per plan.
7. Register edits back to jcm (`register_edit(file_paths=[...])`) after changes.

## 6. Anti-patterns (MUST NOT)

- ❌ Full-file Read via native before any `get_file_outline`.
- ❌ Manual `Grep` for a query that `search_symbols(semantic=true|fusion=true)` + `search_text` already cover.
- ❌ Blind multi-file `Edit` without `check_safe` / `find_referencing_symbols` / `get_blast_radius` first.
- ❌ String substitution via `Edit` when `plan_refactoring` applies.
- ❌ Refactoring (rename/move/extract) without `plan_refactoring` when blast radius > same-file.
- ❌ Skipping diagnostic checks after edits that alter signatures or imports.
- ❌ Searching again after `verdict="no_implementation_found"` — respect the negative evidence.
- ❌ Ignoring `_meta.budget_warning` / `auto_compacted: true` — stop exploring and work with what you have.
- ❌ Manual blast-radius tracing — `get_blast_radius(depth=2, include_source=true)` is instant.
- ❌ Listing files via `Glob` when jcm `get_file_tree` gives the same result faster.

## 7. Token-budget discipline

- `assemble_task_context(token_budget=4000)` — focused tasks.
- `assemble_task_context(token_budget=6000)` + `model=<active>` — large refactor / audit with Sonnet; bump to 10k only on Opus.
- `get_context_bundle(token_budget=6000, budget_strategy="core_first")` — multi-symbol context.
- `search_symbols(token_budget=3000)` + `detail_level="compact"` — broad discovery (~15 tokens/row).
- Always check `_meta.tokens_used` / `_meta.tokens_remaining`. Stop when `budget_warning` appears.
- Choose `budget_strategy="compact"` on `get_context_bundle` for call-chain mapping (signatures only).

## 8. Model-driven tiering (jcm → assemble_task_context)

Pass `model="<id>"` to `assemble_task_context` so jcm can sub-select the cheapest sufficient tool tier for the active model:

- Claude Opus (`claude-opus-4-7`) → fullest tool tier
- Claude Sonnet (`claude-sonnet-4-6`) → expanded tier
- Claude Haiku (`claude-haiku-4-5`) → minimal tier
- GPT-4o / GPT-5 / o1 / Llama → use printed model id

If `assemble_task_context` is not appropriate for a non-code task, call `announce_model(model="...")` once instead.

---
Repo: `animaios/animamunchmcp`
jcm Symbol ID: `{file_path}::{qualified_name}#{kind}`
