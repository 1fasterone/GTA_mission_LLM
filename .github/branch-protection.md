# Main branch protection

`main` is an integration branch. Human, Codex and Dependabot changes follow
the same reviewed pull-request path. Do not bypass CI or push directly to main.

## Observed settings (November 4, 2026)

The active public `Protect main` ruleset (`24466454`) requires:

- A pull request with resolved review threads and squash merging only.
- Zero required approving identities, supporting the solo maintainer workflow.
- Up-to-date branches and passing `Lint (ruff)`, `Type check (mypy)`,
  `Test (pytest)`, `Acceptance (frozen Step 3.5)` and `Test (blender_render)`.
- Linear history; blocked force pushes and branch deletion; no bypass actors.

The separate branch-protection endpoint returns 403 for the integration, so
its additional requirements cannot be independently enumerated here. The
repository policy also requires passing `Generated C# (dotnet build)` and
`Unity generated-project compile` before merge, even when a ruleset read lists
only the five checks above. Preserve every required check and re-read live
policy before merging. An agent review is review evidence, not a GitHub
approving identity.

Unity credentials are configured; #282's Unity compile reached
`CiCompileCheck.Perform` successfully. Earlier missing-license statements are
historical. Each new PR head needs its own successful compile result.

The Actions policy requires full-SHA pinning and narrowly permits
`game-ci/unity-builder@*`. Preserve its pinned action and credential handling.

Audit with:

```powershell
gh api repos/1fasterone/GTA_mission_LLM/rulesets/24466454
gh api repos/1fasterone/GTA_mission_LLM/branches/main/protection
```
