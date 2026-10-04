# CI branch protection

The workflow exposes a dedicated required-check candidate:

```text
Acceptance (frozen Step 3.5)
```

`tests/test_ci_contract.py` fails if `.github/workflows/ci.yml` removes that
job or stops running the frozen Step 3.5 acceptance suite. The test protects
the workflow source; it cannot configure GitHub branch protection.

## Administrator action

A repository administrator must update the protection rule or ruleset for
`main`:

1. Open the repository’s **Settings** → **Branches** (or **Rules** → the
   applicable ruleset).
2. Edit the rule protecting `main` and enable **Require status checks to pass
   before merging**.
3. Add the exact check **Acceptance (frozen Step 3.5)**. If GitHub displays
   workflow-qualified contexts, select **CI / Acceptance (frozen Step 3.5)**.
4. Save the rule and verify that a pull request cannot merge while this check
   is failing or absent.

This administrator step is required because a passing CI job is not a merge
block until its check is selected in branch protection.
