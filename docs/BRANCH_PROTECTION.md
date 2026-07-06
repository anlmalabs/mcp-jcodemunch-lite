# Branch Protection — `main`

## branch_protection signal: PASS

> Created at 2026-07-01 · GH account `vi70x4` (admin=true)
> Last updated: 2026-07-01 15:10 UTC+3 (added enforce-admins ruleset 18377613)

## Current state

- `gh api repos/animaios/animamunchmcp --jq '.permissions.admin'` → `true`
- `gh api repos/animaios/animamunchmcp/rulesets` → three rulesets present, all `enforcement: active`
- Legacy `branches/main/protection` → 404 (superseded by rulesets)

### Applied rulesets

| ID | Name | Enforce admins | Ref |
|----|------|----------------|-----|
| [18377394](https://github.com/animaios/animamunchmcp/rules/18377394) | main — PR reviews + required checks | no | `refs/heads/main` |
| [18377424](https://github.com/animaios/animamunchmcp/rules/18377424) | default branch — enforce for admins + PR reviews | yes | `refs/heads/main` |
| [18377613](https://github.com/animaios/animamunchmcp/rules/18377613) | main — enforce admins on default branch | yes | `refs/heads/main` |

### PR protections (all three rulesets)

- **Pull request required**: 1 approving review, dismiss stale on push, require CODEOWNERS approval, require last-push approval; allowed merges: merge/squash/rebase.
- **Required status checks** (strict, must be up to date): `test`, `lint`, `large-file-check`, `unused-deps-check`, `dead-feature-flags-check`, `check-agents-md`.
- **Non-fast-forward**: disabled (linear history).
- **Bypass actors**: empty — nobody bypasses (admin must pass PR reviews per the two admin-enforcing rulesets).

## CODEOWNERS

`.github/CODEOWNERS` teams referenced:
- `@jcodemunch/maintainers` — README, pyproject.toml, LICENSE, CHANGELOG, CONTRIBUTING, CODEOWNERS, `.github/`.
- `@jcodemunch/developers` — `src/`, `tests/`, `docs/`, `AGENTS.md`, `ARCHITECTURE.md`, `SPEC.md`.

## JSONnet template

```jsonnet
// docs/ruleset-payload.jsonnet — change `name`/`enforce_admins` per intent
local contexts = ["test", "lint", "large-file-check", "unused-deps-check", "dead-feature-flags-check", "check-agents-md"];
{
  name: "<branch> — <intent>",
  target: "branch",
  enforcement: "active",
  conditions: {
    ref_name: {
      include: ["refs/heads/<branch>"],
      exclude: [],
    },
  },
  rules: [
    {
      type: "pull_request",
      parameters: {
        required_approving_review_count: 1,
        dismiss_stale_reviews_on_push: true,
        required_reviewers: [],
        require_code_owner_review: true,
        require_last_push_approval: true,
        required_review_thread_resolution: false,
        allowed_merge_methods: ["merge", "squash", "rebase"],
      },
    },
    {
      type: "required_status_checks",
      parameters: {
        strict_required_status_checks_policy: true,
        do_not_enforce_on_create: false,
        required_status_checks: [ { context: c } for c in contexts ],
      },
    },
    { type: "non_fast_forward" },
  ],
  bypass_actors: [],
  enforce_admins: true,  // ← flip to false for "admins may push"
}
```

## Apply from template

```sh
jsonnet docs/ruleset-payload.jsonnet \
  --ext-str branch=main \
  > /tmp/ruleset.json
gh api -X POST repos/animaios/animamunchmcp/rulesets \
  --input /tmp/ruleset.json --jq '{id, name, enforcement}'
rm -f /tmp/ruleset.json
```

## Manual fallback (no admin access — UI walkthrough)

Settings → Branches → Add rule → `main` →
- [x] Require a pull request before merging
  - [x] Require approvals: 1
  - [x] Dismiss stale pull request approvals when new commits are pushed
  - [x] Require review from Code Owners
- [x] Require status checks to pass before merging
  - [x] Require branches to be up to date before merging
  - contexts: `test`, `lint`, `large-file-check`, `unused-deps-check`, `dead-feature-flags-check`, `check-agents-md`
- [x] Block force pushes
- [x] Do not allow bypassing the above settings
