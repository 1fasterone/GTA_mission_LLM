# RFC-0001: Authority model

Status: Accepted

## Decision

The compiler uses an evidence-to-decision chain:

```text
Prompt -> claims -> IntentLedger -> IntentResolver -> canonical UnitySchema
       -> read-only validation -> composition -> asset binding -> generation
```

Only the named owner may write its artifact. Every downstream stage may read
upstream output but cannot reinterpret or silently repair meaning.

| Domain | Authority | May write | Must not do |
| --- | --- | --- | --- |
| Intent | `src/intent/claims.py` producers | append claims | choose winners or edit schema |
| Resolution | `src/intent/resolver.py` | canonical decisions and provenance | generate code |
| Schema | Step 3 plus schema materialization | typed project IR | invent intent after resolution |
| Composition | Compose Micros | subsystem plan and wiring | override resolved intent |
| Projectiles | resolved intent plus registry | typed projectile schema contract | mutate inside validation |
| Assets | `AssetBinder` | frozen entity-to-asset selections | change entity identity |
| Generation | deterministic generators | files derived from schema | reinterpret prompt text |
| Validation | validators | findings only | mutate schema or add mechanics |
| Testing | tests/evals | evidence and locked expectations | hide skips or rewrite captures |

## Enforcement

- `IntentLedger` is append-only and records source, confidence, and rationale.
- `StageExecutionRegistry` rejects a second execution of any named stage.
- `ValidateSchemaStage` compares the schema before/after every validator and
  raises if a validator writes anything.
- Pipeline snapshots are persisted at Pre-Step3, Post-Step3,
  Post-Resolution, Post-Reconciliation, Post-Compose, and Pre-Generation.
- Mechanics are registered once in `MechanicRegistry`; Fire, Projectile, and
  Health are the pilot registrations.
- Generated C# and a real generated Unity project are mandatory CI gates.

## Consequences

Adding a mechanic requires a schema contract, one registry entry, its runtime
implementation, and tests. Prompt classifiers remain replaceable evidence
sources. A behavior change is traceable through the ledger and snapshot diff.
