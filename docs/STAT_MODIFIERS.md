# Actor stat modifiers

The existing Inventory `stat_boost` effect now reaches an actor-owned
`StatModifier` through its typed `OnStatBoostEffect(float, string)` event.
No schema fields or pipeline LLM calls were added.

Supported names are exact, case-sensitive identifiers:

| Name | Reader | Meaning of magnitude 0.5 |
| --- | --- | --- |
| `speed` | On-foot Controller, AI movement | 1.5 times base speed |
| `armor` | Health, before shield absorption | Incoming damage divided by 1.5 |
| `aggression` | Perception-based guard AI | 1.5 times sight detection range |

Modifiers add together without rewriting base tuning. AI aggression changes
sight acquisition, not attack damage or cooldown. Vehicle/aircraft speed and
pointer controllers do not claim speed support. A multi-mode controller applies
speed to its on-foot mode only.

The composer registers only stats actually read by each actor's components.
Components on the same GameObject share a store. Other actors have separate
stores. A systems Inventory targets the sole composed player; an explicitly
targeted Inventory uses that target. Multiple possible actor GameObjects, no
receiver, a destroyed actor, unknown names, and invalid numeric values produce
`[stat.abstain]` diagnostics instead of choosing an arbitrary receiver. These
are runtime binding checks; they do not rewrite extension fields or discard
schema content. The existing Inventory consumption semantics are unchanged.

`Apply(stat, magnitude, duration = 0, decay = false)` supports permanent effects,
fixed expiry, and linear decay. Duration is seconds of Unity scaled game time;
zero lasts for the actor's lifetime. Repeated applications stack independently.
The existing item schema/event supplies no duration, so ordinary stat-boost
items are permanent. Authored item durations require a future extension or
coordinated schema promotion; they are not inferred from item names.

Composition connects scene components at startup. Dynamically spawned actors or
inventories need the same registration/binding during their spawn lifecycle.
Standalone micro output has optional reader delegates and works with no store.

Validation uses engineer-authored fixtures, not a live pipeline evaluation.
Tests execute the generated C# store against a minimal Unity time adapter and
check generated composition, actor scope, reader expressions, and missing targets.
