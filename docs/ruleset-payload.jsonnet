// GitHub branch-protection ruleset payload (for gh api --input).
// Tune `name`, `enforce_admins`, and `conditions.ref_name.include` per intent.
local contexts = ["test", "lint", "large-file-check", "unused-deps-check", "dead-feature-flags-check", "check-agents-md"];
{
  name: "main — enforce admins",
  target: "branch",
  enforcement: "active",
  conditions: {
    ref_name: {
      include: ["refs/heads/main"],
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
  enforce_admins: true,
}
