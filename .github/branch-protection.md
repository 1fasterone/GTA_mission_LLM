# Main branch protection

This document specifies the Phase 0 target. A file in the repository does not
change GitHub settings. Repository administrators must apply the rules and
verify them in GitHub before the exit criterion is met.

## Current state checked for this change

On 2026-10-29, the active `Protect main` ruleset targeted the default branch,
required the existing CI checks, required pull requests, prohibited force
pushes and branch deletion, and reported no bypass actors. It required **zero**
approving reviews. The repository setting `allow_auto_merge` was false. The
connector used to inspect it cannot modify repository-administration settings;
these observations may have changed since that date.

## Required configuration

Apply to `main` for all contributors, including Codex and Dependabot branches:

1. Require a pull request, at least **one approving review**, and resolved review
   conversations. Dismiss stale approvals when new commits are pushed and
   require approval of the latest reviewable push.
2. Require up-to-date branches and the existing CI contexts:
   `Lint (ruff)`, `Type check (mypy)`, `Test (pytest)`,
   `Acceptance (frozen Step 3.5)` and `Test (blender_render)`.
3. Once the workflows in this change run successfully on a real PR, also require
   **`Generated C# (.NET)`** and **`Unity generated-project compile`**. Check
   the exact context names GitHub displays before saving the rule. Do not mark a
   missing, queued, skipped, cancelled, or failing run as an acceptance pass.
4. Disable repository auto-merge and remove any external automation that merges
   without the required approval and checks. Block direct pushes and force pushes
   to `main`; do not configure a bypass actor for these protections.
5. Keep the existing linear-history, deletion and conversation-resolution rules.

A PR author cannot approve their own PR. Ensure a second eligible reviewer is
available before requiring one approval; otherwise the rule can block all merges.
Configure a Unity license for the hosted Unity workflow through GitHub Actions
secrets: a Personal license uses `UNITY_LICENSE`, `UNITY_EMAIL` and
`UNITY_PASSWORD`; a Professional license uses `UNITY_SERIAL`, `UNITY_EMAIL` and
`UNITY_PASSWORD`. Store the same required names in Dependabot secrets if its PRs
must run the Unity job. Do not put credentials or license contents in the repo.
External fork PRs do not receive repository secrets; they cannot pass the Unity
job until an authorized maintainer runs the change from a trusted branch with
access to the required license. The job must not silently pass in that case.

## Verification after application

- Inspect the effective `main` ruleset and bypass actors in GitHub settings.
- Confirm that a direct push is rejected and a PR without approval cannot merge.
- Confirm that all required check contexts exist on the actual PR and a failed
  or missing check prevents merge.
- Verify an eligible person other than the author can approve, and a new push
  invalidates the stale approval.
- Confirm Codex and Dependabot PRs receive the same policy and no unattended
  auto-merge path bypasses it.

Do not claim the roadmap's branch-protection exit criterion until these checks
have been completed against the live repository settings.
