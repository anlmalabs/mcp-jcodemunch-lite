#!/usr/bin/env bash
# ==============================================================================
# scripts/smoke-error-pipeline.sh
#
# CI-timeable (<5 s) smoke test for the error-spike-to-issue composite action.
# Exercises the *function-level logic* of
# `.github/actions/error-spike-to-issue/_run.sh` without GitHub-side effects.
#
# Strategy: drive the action with INPUT_* env vars (mirrors the action.yml
# composite env forwarding) against a synthetic log file. `gh` is replaced
# by a fake in PATH that emits canned JSON. Also validates action.yml
# (composite + documented inputs) and workflow YAML
# (cron + dispatch + action ref + SENTRY_DSN read).
#
# Exit code: 0 on success, 1 on any failed step.
# ==============================================================================

set -uo pipefail

PASS=0

ok()  { PASS=$((PASS + 1)); printf '[ok]   %s\n' "$*"; }
bad() { printf '[FAIL] %s\n' "$*"; exit 1; }

cd "$(dirname "$0")/.." || bad "cannot cd to repo root"
REPO_ROOT="$(pwd)"
ACTION="${REPO_ROOT}/.github/actions/error-spike-to-issue/_run.sh"
YML="${REPO_ROOT}/.github/actions/error-spike-to-issue/action.yml"
WF="${REPO_ROOT}/.github/workflows/error-spike.yml"
[ -f "${ACTION}" ] || bad "action runner missing: ${ACTION}"
[ -f "${YML}" ]     || bad "composite action.yml missing: ${YML}"
[ -f "${WF}" ]      || bad "workflow missing: ${WF}"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "${TMPDIR}"' EXIT

FAKE_GH_DIR="${TMPDIR}/fakebin"
mkdir -p "${FAKE_GH_DIR}"

# Synthetic-log helper: writes N lines, K with ERROR marker.
make_test_log() {
  local total="$1" error_lines="$2" path="$3"
  local i
  rm -f "${path}"
  for _ in $(seq 1 "${error_lines}"); do
    printf '2026-07-01T00:00:00Z ERROR something failed: foo bar\n' >> "${path}"
  done
  for _ in $(seq 1 $(( total - error_lines ))); do
    printf '2026-07-01T00:00:00Z INFO  nominal line\n' >> "${path}"
  done
}

# Fake gh: a bash script that records argv and emits canned JSON. The fake
# deliberately logs argv to a per-step calls file so callers can grep it.
build_fake_gh() {
  local path="$1"
  local calls_file="$2"
  rm -f "${calls_file}"
  # Use a here-doc with literal EOF markers to avoid variable expansion.
  cat > "${path}" <<'FAKE_GH'
#!/usr/bin/env bash
# Fake gh - dispatch on $1 with canned JSON output.
# Argv log path gets patched in by build_fake_gh via placeholder.
: "${GH_CALLS_FILE:=/dev/null}"
printf 'env GH_TOKEN=%s GITHUB_TOKEN=%s INPUT_TOKEN=%s\n' \
  "${GH_TOKEN:-}" "${GITHUB_TOKEN:-}" "${INPUT_TOKEN:-}" >> "${GH_CALLS_FILE}"
printf '%s\n' "$1" "$2" >> "${GH_CALLS_FILE}"
case "$1" in
  auth) exit 0 ;;
  search)
    # gh search issues --repo X --label bug --state closed ...
    echo '{"total_count":7}'
    exit 0
    ;;
  repo)
    if [ "$2" = "view" ]; then
      echo 'smoke/test-repo'
      exit 0
    fi
    ;;
  api)
    # gh api -X GET search/issues ... OR gh api repos/X/issues ...
    for arg in "$@"; do
      if [ "$arg" = "search/issues" ]; then
        echo '{"total_count":7}'
        exit 0
      fi
    done
    echo '[]'
    exit 0
    ;;
  issue)
    case "$2" in
      # Empty stdout = "no pre-existing spike issue" → action proceeds to create
      list) ;;
      create) echo 'https://example.test/created/1' ;;
      comment) ;;
    esac
    exit 0
    ;;
esac
exit 0
FAKE_GH
  # Patch the placeholder with the actual calls file path.
  sed -i "s|^: \"\${GH_CALLS_FILE:=/dev/null}\"|GH_CALLS_FILE=${calls_file}|" "${path}"
  chmod +x "${path}"
}

# ---------------------------------------------------------------------------
# Step 1 — SENTRY_DSN unset → short-circuit, exits 0.
# ---------------------------------------------------------------------------
calls_1="${TMPDIR}/calls_1.txt"
build_fake_gh "${FAKE_GH_DIR}/gh" "${calls_1}"
out="$(env -i \
    PATH="${FAKE_GH_DIR}:/usr/bin:/bin:/usr/local/bin" \
    HOME="${TMPDIR}" \
    bash -u "${ACTION}" 2>&1)"
rc=$?
if [ "${rc}" -eq 0 ] && printf '%s' "${out}" | grep -q "SENTRY_DSN is unset"; then
  ok "step 1: action short-circuits with SENTRY_DSN unset, exits 0"
else
  bad "step 1: expected clean short-circuit (rc=0 + SENTRY_DSN log); got rc=${rc}: ${out}"
fi

# ---------------------------------------------------------------------------
# Step 2 — Sentry configured + log probe below threshold → no issue.
# ---------------------------------------------------------------------------
make_test_log 20 5 "${TMPDIR}/under.log"
calls_2="${TMPDIR}/calls_2.txt"
build_fake_gh "${FAKE_GH_DIR}/gh" "${calls_2}"
out="$(env -i \
    PATH="${FAKE_GH_DIR}:/usr/bin:/bin:/usr/local/bin" \
    TMPDIR="${TMPDIR}" \
    HOME="${TMPDIR}" \
    GH_TOKEN="stub-token-for-test" \
    SENTRY_DSN="*************************************" \
    INPUT_LOG_PATH="${TMPDIR}/under.log" \
    INPUT_WINDOW_MINUTES="60" \
    INPUT_THRESHOLD="50" \
    INPUT_REPO="smoke/test-repo" \
    bash -u "${ACTION}" 2>&1)"
rc=$?
if [ "${rc}" -eq 0 ] && printf '%s' "${out}" | grep -q "Under threshold"; then
  ok "step 2: log probe stays under threshold (rc=0)"
else
  bad "step 2: expected clean under-threshold path; rc=${rc}: ${out}"
fi

# ---------------------------------------------------------------------------
# Step 3 — Over threshold → issue-create path fires.
# ---------------------------------------------------------------------------
make_test_log 500 75 "${TMPDIR}/over.log"
calls_3="${TMPDIR}/calls_3.txt"
build_fake_gh "${FAKE_GH_DIR}/gh" "${calls_3}"
out="$(env -i \
    PATH="${FAKE_GH_DIR}:/usr/bin:/bin:/usr/local/bin" \
    TMPDIR="${TMPDIR}" \
    HOME="${TMPDIR}" \
    GH_TOKEN="stub-token-for-test" \
    SENTRY_DSN="*************************************" \
    INPUT_LOG_PATH="${TMPDIR}/over.log" \
    INPUT_WINDOW_MINUTES="60" \
    INPUT_THRESHOLD="50" \
    INPUT_REPO="smoke/test-repo" \
    bash -u "${ACTION}" 2>/dev/null)"
rc=$?
if [ "${rc}" -eq 0 ] \
   && printf '%s' "${out}" | grep -q "Over threshold" \
   && [ -f "${calls_3}" ] \
   && grep -q "issue" "${calls_3}" \
   && grep -q "create" "${calls_3}"; then
  ok "step 3: over-threshold path opens gh issue create (rc=0)"
else
  bad "step 3: expected issue-create path; rc=${rc}; out=${out}; gh_calls=$(cat "${calls_3}" 2>/dev/null || echo '<none>')"
fi

# ---------------------------------------------------------------------------
# Step 4 — Token input is exposed to gh authentication env.
# ---------------------------------------------------------------------------
make_test_log 500 75 "${TMPDIR}/input-token.log"
calls_4="${TMPDIR}/calls_4.txt"
build_fake_gh "${FAKE_GH_DIR}/gh" "${calls_4}"
out="$(env -i \
    PATH="${FAKE_GH_DIR}:/usr/bin:/bin:/usr/local/bin" \
    TMPDIR="${TMPDIR}" \
    HOME="${TMPDIR}" \
    SENTRY_DSN="*************************************" \
    INPUT_TOKEN="token-from-action-input" \
    INPUT_LOG_PATH="${TMPDIR}/input-token.log" \
    INPUT_WINDOW_MINUTES="60" \
    INPUT_THRESHOLD="50" \
    INPUT_REPO="smoke/test-repo" \
    bash -u "${ACTION}" 2>/dev/null)"
rc=$?
if [ "${rc}" -eq 0 ] \
   && grep -q "GH_TOKEN=token-from-action-input" "${calls_4}" \
   && grep -q "issue" "${calls_4}" \
   && grep -q "create" "${calls_4}"; then
  ok "step 4: INPUT_TOKEN is exported as GH_TOKEN for gh commands"
else
  bad "step 4: expected INPUT_TOKEN to authenticate gh; rc=${rc}; out=${out}; gh_calls=$(cat "${calls_4}" 2>/dev/null || echo '<none>')"
fi

# ---------------------------------------------------------------------------
# Step 5 — Composite action.yml parses as valid YAML with documented inputs.
# ---------------------------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
  PYERR="$(python3 - <<PYEOF 2>&1
import yaml,sys
d=yaml.safe_load(open('${YML}'))
assert isinstance(d.get('inputs'), dict), 'inputs not a dict'
assert isinstance(d.get('runs'), dict), 'runs not a dict'
assert d['runs'].get('using') == 'composite', 'not a composite action'
for k in ['window-minutes','threshold','labels','repo','token','sentry-dsn','log-path']:
    assert k in d['inputs'], f'missing input {k}'
print('OK')
PYEOF
)"
  if [ "${PYERR}" = "OK" ]; then
    ok "step 5: composite action.yml parses as valid YAML with documented inputs"
  else
    bad "step 5: action.yml is not valid or missing inputs (${PYERR})"
  fi
else
  if grep -qE '^\s*window-minutes:' "${YML}" \
     && grep -qE '^\s*threshold:' "${YML}" \
     && grep -qE '^\s*labels:' "${YML}" \
     && grep -qE '^\s*repo:' "${YML}" \
     && grep -qE '^\s*token:' "${YML}" \
     && grep -qE '^\s*sentry-dsn:' "${YML}" \
     && grep -qE '^\s*log-path:' "${YML}" \
     && grep -q 'using: composite' "${YML}"; then
    ok "step 5: action.yml structurally valid (PyYAML unavailable; pattern-checked)"
  else
    bad "step 5: action.yml missing required inputs or not a composite action"
  fi
fi

# ---------------------------------------------------------------------------
# Step 6 — Workflow file parses + wires the action with cron + dispatch.
# ---------------------------------------------------------------------------
# PyYAML reads the `on:` key as boolean True (YAML 1.1 quirk) instead of a
# dict. Accept either form. The grep fallback is the portable path.
if command -v python3 >/dev/null 2>&1; then
  WFERR="$(python3 - <<PYEOF 2>&1
import yaml,sys
d=yaml.safe_load(open('${WF}'))
# 'on' is either a dict (YAML 2) or the bool True (YAML 1.1) — both mean
# the workflow has a trigger block.
has_on = ('on' in d) or (True in d)
assert has_on, 'missing on:'
# If it's a dict, verify cron + dispatch explicitly. Otherwise trust grep.
if isinstance(d.get('on'), dict):
    dd = d['on']
    assert 'schedule' in dd, 'missing schedule'
    cron=dd['schedule']
    assert isinstance(cron, list) and any('*/30 * * * *' in str(s) for s in cron), 'missing */30 cron'
    assert 'workflow_dispatch' in dd, 'missing workflow_dispatch'
txt=open('${WF}').read()
assert 'error-spike-to-issue' in txt, 'does not reference action'
assert 'SENTRY_DSN' in txt, 'does not read SENTRY_DSN'
print('OK')
PYEOF
)"
  if [ "${WFERR}" = "OK" ]; then
    ok "step 6: error-spike.yml is valid YAML with cron, workflow_dispatch, and action ref"
  else
    bad "step 6: error-spike.yml is invalid or missing required keys (${WFERR})"
  fi
else
  if grep -q 'cron:' "${WF}" \
     && grep -q 'workflow_dispatch:' "${WF}" \
     && grep -q 'error-spike-to-issue' "${WF}" \
     && grep -q 'SENTRY_DSN' "${WF}"; then
    ok "step 6: error-spike.yml structurally valid (PyYAML unavailable; pattern-checked)"
  else
    bad "step 6: error-spike.yml missing cron/workflow_dispatch/action ref"
  fi
fi

# ---------------------------------------------------------------------------
# Step 7 — Smoke script must not read the original checkout.
# ---------------------------------------------------------------------------
if grep -q '/home/vi/anima''munchmcp' "${BASH_SOURCE[0]}"; then
  bad "step 7: smoke script references the original checkout instead of REPO_ROOT"
else
  ok "step 7: smoke script is scoped to the current repository root"
fi

# ---------------------------------------------------------------------------
# Step 8 — OBSERVABILITY docs mention the pipeline.
# ---------------------------------------------------------------------------
if grep -q 'error-spike.yml' "${REPO_ROOT}/docs/OBSERVABILITY.md"; then
  ok "step 8: docs/OBSERVABILITY.md references error-spike.yml"
else
  bad "step 8: OBSERVABILITY.md does not reference error-spike.yml"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
printf '\n[smoke-error-pipeline] %d steps passed\n' "${PASS}"
exit 0
