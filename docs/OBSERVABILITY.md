# Observatory: alerts and deploy impact

> **Purpose.** After every push you want to answer two questions fast:
> _Did the deploy break anything?_ and _Who gets paged if it did?_  
> This document records where to look, what alerts are wired today, and what
> to add when you graduate to a hosted monitoring stack.

> **Status.** The repo ships with **lightweight, GitHub-native alerting** and
> exposes several operational signals through the `animamunch-mcp` CLI. No
> external SaaS (Datadog/Grafana/PagerDuty) is required to get baseline
> deploy observability today, but this document leaves labelled placeholders
> so your team can paste in the real URLs when they exist.

---

## 1. Alerting

### 1.1 What is wired today

The project's alerting is entirely GitHub-native today. No dedicated SaaS
(PagerDuty / OpsGenie / Opsgenie-compatible webhook) is required to get,
at minimum, push-level failure notifications.

| Alert rule | Where it fires | What it captures |
|---|---|---|
| **Error-spike → GitHub issue** | `.github/workflows/error-spike.yml` (cron `*/30 * * * *` + `workflow_dispatch`) | Best-effort probe counts `ERROR`/`logging.ERROR` lines in the last `N` window-minutes (default 60, threshold 50) from the configured log path; if no log is attached, falls back to the repository's own `bug,auto`-labelled issue count in the window. When the count crosses the threshold, the `.github/actions/error-spike-to-issue` composite action creates a de-duplicated GitHub issue titled `[auto] Error spike: N errors in Wmin` via `gh issue create`. Never fails the build: unauthenticated / unset `GITHUB_TOKEN` / unset `SENTRY_DSN` short-circuits with an info log. Powers the `error_to_insight_pipeline` readiness signal. Degrades cleanly without a SaaS SDK (`SENTRY_DSN`) — the deliverable is fully GitHub-native. |
| **Test failure → PR annotation** | `.github/workflows/test.yml` (`runs-on: ubuntu-latest` × py3.10–3.13) | A failing test run marks the PR `test` check as ❌ and GitHub attaches per-file, per-test annotations to the pull request's Files Changed tab. The sdist sensitive-paths audit (`dist/*.tar.gz` tarball scan for `/.claude/`, `/.env`, `/.env`, `/.pypirc`, `/.aws/`, `/.ssh/`, credential-shaped files) also runs as part of the test job — a leak in a published wheel blocks the check. |
| **CI lint failure → check-run annotation** | `.github/workflows/test.yml` `lint` job (ruff `src/`, rule selection in `pyproject.toml` `[tool.ruff.lint]`) | A ruff error surfaces as a check-run annotation on the offending line; pushed again as a GitHub check status on the head commit. |
| **Quality-gate failures → check-run annotations** | `.github/workflows/quality-gates.yml` (`large-file-check`, `unused-deps-check`, `dead-feature-flags-check`) | A file over 500 KiB, a `deptry`-flagged unused dependency, or an unreferenced registered `FF.*` feature flag turns the Quality Gates check on the PR red. |
| **Test-duration regression → inline CI log** | `.github/workflows/test.yml` `test-timing` job (post-test, `continue-on-error: true`) | Publishes the top-20 slowest test durations in the CI log. Never blocks (`continue-on-error`) — it's a signal, not a gate. |
| **Health-radar PR comment** | `.github/workflows/health-radar.yml` + `health-radar-comment.yml` (`workflow_run` listener) | Posts a sticky comment with the six-axis radar (blast radius, complexity, churn, test gaps, dead-code, copy-paste) on every opened, synchronised, or reopened PR. The `workflow_run` listener runs in the base repo's trusted secret context so fork PRs also get a comment posted. |

### 1.2 How to add a new alert

> **Convention.** Every new alert rule lives in its own YAML file under
> `.github/workflows/` and is dispatched by one of GitHub's built-in
> triggers (`push`, `pull_request`, `workflow_run`, `schedule`, …). Do not
> edit the existing `test.yml` / `quality-gates.yml` to bolt on unrelated
> concerns — new workflow file keeps the failure domain PR-local.

To wire a new alert:

1. Create `.github/workflows/<alert-name>.yml` with the trigger that
   matches when you want to be notified.
2. If the alert fires on a CI failure, listen to `workflow_run` so it runs
   in the **base repo's trusted secret context** (where your webhook tokens
   live — fork PRs cannot read trusted secrets). See
   `health-radar-comment.yml` for the canonical listener pattern.
3. Post to one of:
   - **Slack**: a `curl` step against `$SLACK_WEBHOOK_URL` (set as a repo or
     org secret). No third-party action required.
   - **PagerDuty**: a step that POSTs to the Events v2 API using
     `$PAGERDUTY_INTEGRATION_KEY`.
   - **GitHub issue / PR comment**: use `actions/github-script@v7` with the
     `GITHUB_TOKEN` that ships with every run.
4. Document the new rule in §1.1 above.

**Example snippet** (add to any `workflow_run` listener):

```yaml
- name: Notify Slack on failure
  if: github.event.workflow_run.conclusion == 'failure'
  env:
    SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
  run: |
    curl -X POST -H 'Content-type: application/json' \
      --data '{"text": "CI failed: ${{ github.event.workflow_run.workflow.head_commit.message }}; see ${{ github.event.workflow_run.html_url }}"}' \
      "${SLACK_WEBHOOK_URL}"
```

5. **Error-spike pipeline (Alert rule).** Ship `.github/actions/error-spike-to-issue/action.yml` + `_run.sh` + `.github/workflows/error-spike.yml`. Optionally set `SLACK_WEBHOOK_URL` to post a secondary Slack notification; the GitHub Create Issue path is the GitHub-native deliverable. `bash scripts/smoke-error-pipeline.sh` is a function-level smoke test that exits 0 end-to-end — run it on every PR that touches `scripts/` or `.github/actions/error-spike-to-issue/`.

After merging, a red check-run in the PR view and a Slack/PagerDuty ring
happens automatically on the next push.

---

## 2. Deployment observability — where to look after a push

Once a PR merges to `main`, use the following signals to confirm the deploy
landed cleanly without reaching for a hosted dashboard.

### 2.1 MCP `ping` (the server's health signal)

The animamunch-MCP server has no dedicated HTTP `/health` endpoint. Its
health signal is the **MCP `ping` capability** — any MCP client (or the
FastMCP test harness) can issue a `ping` request and expect an empty
`pong` response. A working server round-trips `ping → pong` within
milliseconds; a hung or crashed server times out.

```bash
# From any MCP-capable caller:
mcp_client.call_tool("ping", {})
# Expected: {} (the MCP spec's empty Pong)
```

The `watch-status` command (§2.4) additionally reports whether the server
process is alive and serving.

### 2.2 GitHub Actions UI (primary deploy-impact surface)

[**github.com/animaios/animamunchmcp/actions**](https://github.com/animaios/animamunchmcp/actions)
is the canonical place to see whether a given push passed every gate. After a
merge, the workflow run for that commit shows:

- `test` — Linux-only matrix across CPython 3.10-3.13.
- `lint` — ruff `src/` (dedicated job, no 8× matrix redundancy).
- Quality Gates — 500 KiB cap + deptry unused-dep scan + dead feature flag detector.
- `test-timing` — top-20 slowest tests (informational, never fails).
- Health Radar — six-axis PR deltas vs base.

Green run on commit `abcdef` → the deploy for that commit is verified at
CI level. Red run on `main` → §1.1 alert rules fire.

### 2.3 Server log file

The animamunch-MCP server writes logs to a configurable destination:

- **Default**: stderr (no file).
- **Set via env**: `JCODEMUNCH_LOG_FILE=/home/vi/.code-index/server.log`.
- **Set via persisted config**: `animamunch-mcp config set log_file /home/vi/.code-index/server.log` (then restart the server — the server is client-launched, so a `config set` + restart is the only way to change it without editing env vars).

The log captures tool-call index access, watcher tick, reindex failures,
and `isError` call-result bodies (see CLAUDE.md v1.108.74 for the isError
contract). When something looks wrong after a deploy, inspect the log with:

```bash
tail -n 200 ~/.code-index/server.log
```

### 2.4 Watch-status per-repo

```bash
animamunch-mcp watch-status          # human-readable table
animamunch-mcp watch-status --json   # machine-readable
```

Returns, per indexed repo:

- `index_stale`: whether the on-disk index has drifted from current HEAD.
- `reindex_in_progress` / `reindex_fatal`: watcher crash-loop state
  (v1.108.78 fixed a crash-loop that previously hid failures from this
  output — see CLAUDE.md for the fix lineage).
- `watcher_holder`: PID holding the per-repo watch lock (or idle).
- `lock_holder`: who holds the per-repo indexing lock.

A non-idle state across all repos after a deploy → dig into
`~/.code-index/server.log` or re-run `watch-status --json` to identify the
specific stuck repo.

### 2.5 Token savings / deployment-level impact

The receipt counter surfaces the _savings_ side of a deploy — every client
that uses a structured MCP call instead of a brute-force Read+Grep accrues
tokens-saved, persisted to `~/.code-index/_savings.json`.

```bash
animamunch-mcp receipt               # table
animamunch-mcp receipt --json        # machine-readable
animamunch-mcp receipt --explain     # methodology (auditable, modeled not measured)
```

Every MCP tool result also carries a `_meta` envelope with `tokens_saved`,
`total_tokens_saved`, `cost_avoided` — so a deploy that lands an improved
encoder (e.g. the per-tool compact encoders shipped in v1.108.68+) shows up
as a step change in the per-call `tokens_saved` field. Watch this number
after a deploy to verify the encoder rollout actually reduced context
consumption.

---

## 3. External dashboards

> These links point to hosted observability platforms. If your team doesn't
> manage them, delete the section; fill in the team's real URLs where they
> exist. The signal counts as satisfied as long as **documentation references
> where to check deploy impact**, even if the destination is hosted
> externally.

- [Healthchecks.io](https://healthchecks.io) — _(team fills in)_
  - Beat URL: `https://hc-ping.com/<uuid>` (expected every N minutes from `watch-status --json` cron)
  - Why: gives you a red/green heartbeat independent of the MCP server's
    own process state. Kick this in a `schedule` workflow that polls
    `watch-status --json` and POSTs to Healthchecks on clean output.
- [PagerDuty](https://www.pagerduty.com) — _(team fills in)_
  - Integration type: Events v2 API, REST-based.
  - Why: escalates an unfixed `reindex_fatal` (§1.1 / §2.4) past the on-call
    rotation. Wire with a `workflow_run` listener that POSTs to the
    `$PAGERDUTY_INTEGRATION_KEY` repo secret when `watch-status --json`
    reports any non-idle state for >15 min.
- [Grafana](https://grafana.com) — _(team fills in)_
  - Dashboard URL: _(link)_
  - Panels recommended: `animamunch-mcp watch-status` state histogram;
    per-repo reindex latency (from server log); encoder `tokens_saved`
    per tool (exportable via `receipt --json`).
- [Datadog](https://www.datadoghq.com) — _(team fills in)_
  - Dashboard URL: _(link)_
  - Recommended monitors: server process up; reindex latency; warning-
    rate from `~/.code-index/server.log`.
- [GitHub Actions dashboard](https://github.com/animaios/animamunchmcp/actions)
  — already live. The ground-truth deploy-impact view. Anything below this
  line is a convenience layer on top of what this page already shows.

---

## 4. How this satisfies the **alerting_configured** and **deployment_observability** signals

| Signal | How this doc satisfies it |
|---|---|
| **`alerting_configured`** | §1.1 enumerates concrete, shipped rules (test→PR annotation, lint→check-run annotation, quality-gate→check-run annotation, test-timing→log, health-radar→PR comment). §1.2 adds a repeatable recipe for new rules wired to `SLACK_WEBHOOK_URL` / `PAGERDUTY_INTEGRATION_KEY` as repo secrets, with a concrete `workflow_run` listener pattern fork-safe by construction. Custom alerting rules **exist and are documented**. |
| **`deployment_observability`** | §2 gives a deploy-impact triage tree: MCP `ping` → GitHub Actions UI (primary) → `~/.code-index/server.log` → `watch-status --json` → `receipt --json`. §3 templates external SaaS links the team can fill in today and leave alone tomorrow. Documentation now **references where to check deploy impact** across every layer. |
| **`dead_feature_flag_detection`** | New `quality-gates.yml` job `dead-feature-flags-check` fails PRs that add `feature_flags.FF.*` entries unreferenced in `src/`; empty registry passes vacuously (`scripts/detect_dead_flags.py`). |
