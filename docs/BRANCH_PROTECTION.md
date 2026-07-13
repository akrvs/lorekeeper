# Branch protection — require CI to pass before merge

Enforce that the CI workflow's two checks (`lint` and `test` from
`.github/workflows/ci.yml`) must pass before any PR can merge into `main` or
`develop`.

> The status-check **context names** are the workflow job names: `lint` and `test`.

## Option A — GitHub CLI (`gh`)

Run once per branch (replace `OWNER/REPO`):

```bash
for BRANCH in main develop; do
  gh api -X PUT "repos/OWNER/REPO/branches/${BRANCH}/protection" \
    --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "checks": [
      { "context": "lint" },
      { "context": "test" }
    ]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
done
```

- `strict: true` — a PR must be up to date with the base branch before merging
  (re-runs CI on the merge result).
- `enforce_admins: true` — even admins cannot bypass the checks.
- Adjust `required_approving_review_count` to taste (set the review block to
  `null` to require only CI, not human review).

Verify:

```bash
gh api repos/OWNER/REPO/branches/main/protection/required_status_checks
```

## Option B — GitHub UI

1. **Settings → Branches → Add branch ruleset** (or *Add classic branch protection rule*).
2. Branch name pattern: `main` (repeat for `develop`).
3. Enable **Require status checks to pass before merging** and **Require branches
   to be up to date before merging**.
4. In the search box add the checks: **`lint`** and **`test`**.
5. (Recommended) **Require a pull request before merging**, **Do not allow
   bypassing the above settings**, and **Require linear history**.
6. Save.

## Notes
- The checks only appear in the picker after the workflow has run at least once
  on the repo, so push the branch (or open a PR) first.
- These names must stay in sync with the `jobs:` keys in `ci.yml`. If you rename
  a job, update the required check here.
