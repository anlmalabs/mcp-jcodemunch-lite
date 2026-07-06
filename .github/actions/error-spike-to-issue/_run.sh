#!/usr/bin/env bash
#
# error-spike-to-issue — best-effort error-rate probe + GitHub issue creation.
#
# Inputs (env vars forwarded by action.yml):
#   INPUT_WINDOW_MINUTES   — look-back window (default 60)
#   INPUT_THRESHOLD        — error count that triggers an issue (default 50)
#   INPUT_REPO             — target repo slug (org/repo), e.g. "animaios/animamunchmcp"
#   INPUT_TOKEN            — GitHub token; may be unset if run without GITHUB_TOKEN
#   INPUT_LOG_PATH         — optional local log file to probe
#   INPUT_LABELS           — comma-separated issue labels (default "bug,auto")
#   SENTRY_DSN             — optional; set only if a SaaS SDK is configured. The
#                             pipeline works without it.
#
# Behavior:
#   Step 1 — short-circuit log when SENTRY_DSN is unset (no SDK configured).
#   Step 2 — source error tracking: try the optional INPUT_LOG_PATH first;
#            otherwise fall back to polling GitHub's `search/issues` API for
#            recently closed auto/bug issues within the window.
#   Step 3 — compare against INPUT_THRESHOLD and create a de-duped issue via
#            `gh issue create` if the count crosses the threshold.
#
# Safety:
#   - All operations are best-effort. Any failure emits an info/warn line but
#     exits 0 so a misconfigured pipeline never fails the build.
#   - No token or webhook URL is hard-coded here.

set -euo pipefail

WINDOW_MINUTES="${INPUT_WINDOW_MINUTES:-60}"
THRESHOLD="${INPUT_THRESHOLD:-50}"
REPO="${INPUT_REPO:-}"
TOKEN="${INPUT_TOKEN:-${GH_TOKEN:-${GITHUB_TOKEN:-}}}"
LOG_PATH="${INPUT_LOG_PATH:-}"
LABELS="${INPUT_LABELS:-bug,auto}"
ERRORS_SEEN=0

if [ -n "$TOKEN" ]; then
  export GH_TOKEN="$TOKEN"
fi

echo "[info] window=${WINDOW_MINUTES}m threshold=${THRESHOLD} repo=${REPO:-<unset>}"

# ----------------------------------------------------------------------------
# Step 1 — SaaS SDK opt-in. The repository plumbs Sentry/Bugsnag/Rollbar via
# SENTRY_DSN / BUGSNAG_API_KEY / ROLLBAR_ACCESS_TOKEN. The GitHub-native
# deliverable works without any of these, but we surface an info banner.
# ----------------------------------------------------------------------------
if [ -z "${SENTRY_DSN:-}" ] && [ -z "${BUGSNAG_API_KEY:-}" ] && [ -z "${ROLLBAR_ACCESS_TOKEN:-}" ]; then
  echo "[info] SENTRY_DSN is unset — skipping SaaS probe; using GitHub-native path."
fi

# ----------------------------------------------------------------------------
# Step 2a: log-file probe (override for deployments that funnel server logs
#          into artifacts the action can read locally).
# ----------------------------------------------------------------------------
probe_log() {
  if [ -z "$LOG_PATH" ]; then
    echo "[info] no INPUT_LOG_PATH provided — skipping log-based probe"
    return 0
  fi
  if [ ! -f "$LOG_PATH" ]; then
    echo "[warn] INPUT_LOG_PATH=$LOG_PATH does not exist — skipping log-based probe"
    return 0
  fi
  local cnt
  cnt="$(grep -cE '(^|[^a-zA-Z])(ERROR|logging\.ERROR|level=ERROR)([^a-zA-Z]|$)' \
           "$LOG_PATH" 2>/dev/null || echo 0)"
  echo "[ok] log probe found ${cnt} ERROR lines in ${LOG_PATH}"
  ERRORS_SEEN=$cnt
}

# ----------------------------------------------------------------------------
# Step 2b: GitHub-API fallback — best-effort recency probe via `gh search issues`
# ----------------------------------------------------------------------------
probe_github_api() {
  if [ -z "$REPO" ]; then
    echo "[warn] REPO unset — skipping GitHub API probe"
    return 0
  fi
  if ! command -v gh &>/dev/null; then
    echo "[warn] gh CLI missing — skipping GitHub API probe"
    return 0
  fi
  local created_since
  created_since="$(date -u -d "${WINDOW_MINUTES} minutes ago" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null \
                    || date -u -v-"${WINDOW_MINUTES}M" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null \
                    || echo '')"
  if [ -z "$created_since" ]; then
    echo "[warn] date command could not compute the window — skipping API probe"
    return 0
  fi
  # query = closed, auto+bug-labeled issues created inside the window
  local gh_out count
  gh_out="$(gh search issues \
      --repo "$REPO" \
      --match issue \
      --label bug \
      --label auto \
      --state closed \
      --created ">=$created_since" \
      --limit 100 \
      --json number 2>/dev/null || echo '[]')"
  count="$(printf '%s' "$gh_out" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)"
  echo "[ok] GitHub API probe: ${count} auto/bug closed issues since ${created_since}"
  if [ "$count" -gt 0 ]; then
    ERRORS_SEEN=$(( ERRORS_SEEN > count ? ERRORS_SEEN : count ))
  fi
}

# ----------------------------------------------------------------------------
# Step 3: de-duplicate + create issue
# ----------------------------------------------------------------------------
create_issue_if_threshold_met() {
  if [ "$ERRORS_SEEN" -lt "$THRESHOLD" ]; then
    echo "[ok] Under threshold (${ERRORS_SEEN} < ${THRESHOLD}) — no issue created"
    return 0
  fi
  if ! command -v gh &>/dev/null; then
    echo "[warn] gh CLI missing — cannot create issue"
    return 0
  fi
  echo "[warn] Over threshold (${ERRORS_SEEN} >= ${THRESHOLD}) — creating auto issue"

  # Repository override: if REPO is set we pass --repo; otherwise gh infers
  # from the current directory's git remote (works in actions/checkout worktree).
  local repo_flag=()
  if [ -n "$REPO" ]; then
    repo_flag=(--repo "$REPO")
  fi

  # De-dup: don't re-create if an open auto error-spike issue exists.
  local existing
  existing="$(gh issue list \
      "${repo_flag[@]}" \
      --state open \
      --label bug \
      --label auto \
      --search "[auto] Error spike" \
      --json number \
      --jq '.[0].number' 2>/dev/null || echo '')"
  if [ -n "$existing" ]; then
    echo "[ok] open auto error-spike issue #${existing} already exists — not duplicating"
    return 0
  fi

  # Turn comma-separated labels into repeated --label flags. Only --label is
  # the documented gh-issue-create input.
  local label_flags=()
  while IFS= read -r -d '' label || [ -n "$label" ]; do
    [ -z "$label" ] && continue
    label_flags+=(--label "$label")
  done < <(printf '%s' "$LABELS" | tr ',' '\0' 2>/dev/null)

  gh issue create \
    "${repo_flag[@]}" \
    "${label_flags[@]}" \
    --title "[auto] Error spike: ${ERRORS_SEEN} errors in ${WINDOW_MINUTES}min" \
    --body "Best-effort probe detected **${ERRORS_SEEN} errors** in the last **${WINDOW_MINUTES} minutes** (threshold **${THRESHOLD}**). This issue was created automatically by the \`error-spike-to-issue\` action in \`.github/workflows/error-spike.yml\`.

Next steps:
1. Examine \`~/.code-index/server.log\` (or \`INPUT_LOG_PATH\`) for the window.
2. If the spike is a false positive, tune \`threshold\` in the workflow inputs; close this issue.
3. If real, triage and update labels with the appropriate severity."
  echo "[ok] auto error-spike issue created"
}

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
main() {
  probe_log
  probe_github_api
  create_issue_if_threshold_met
}

main "$@"
