# Main branch protection

`main` is an integration branch. Codex, human, and Dependabot changes follow
the same pull-request path.

Required repository settings:

- Require a pull request before merging.
- Require one approving review and dismiss stale approvals.
- Require branches to be up to date.
- Require `Lint (ruff)`, `Type check (mypy)`, `Test (pytest)`,
  `Acceptance (frozen Step 3.5)`, `Generated C# (dotnet build)`, and
  `Unity generated-project compile`.
- Apply the rules to administrators and block force pushes/deletions.
- Disable auto-merge and direct pushes to `main`.

Applied to the GitHub `main` branch on 2026-10-30. The Unity status is
intentionally mandatory. The repository still needs a `UNITY_LICENSE` secret;
until it is configured, the workflow fails loudly and merge remains blocked
instead of silently skipping compilation.

The repository Actions policy requires full-SHA pinning, allows GitHub-owned
actions, and narrowly allowlists `game-ci/unity-builder@*` as the only
third-party action needed by the Unity compile gate.

Audit with:

```powershell
gh api repos/1fasterone/GTA_mission_LLM/branches/main/protection
gh api repos/1fasterone/GTA_mission_LLM/actions/permissions
```
