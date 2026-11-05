# Rendered component API: S1 shadow extraction

S0 is integrated by #283 (`68ef096`). S1 adds read-only extraction; S2 must
separately switch consumers and remove the three copied `_COMPOSED_*` tables.
No gameplay ownership migration is established by either source parsing or a
registration entry.

## Authority and entry points

`src/unity_component_api.py:extract_components` accepts the existing selected
Subsystem list and composer. It clones selection, removes authoring channels
before schema/default-fix, calls the production `MicroComposer._resolve` and each
micro's `write_api_sources` hook, then extracts every emitted C# helper and primary
class. Deterministic writers reuse `write_files`; asset-producing writers bypass
synthesis through their own source-inspection hook. It does not reinterpret a prompt, call an LLM, mutate the input selection,
or invent schema fields. Sprite inspection shares the exact C# renderer used by
normal generation and never calls its image backend. Audio and NPC-dialog
inspection suppress synthesis and batching, restoring prior writer flags even
on failure. Normal production generation continues to fulfill its assets.

Writer-owned `api_baseline_options` hooks also remove nested authored C# before
default-fix: FSM entry/update/exit bodies and transition conditions, plus animator
driving expressions. State/edge identities and terminal markers remain; driven
animator parameters retain their driven status, so inspection does not invent a
public setter. Only cloned inspection options are neutralized. Production source
and saved selection retain the original authored bodies. The fragment cannot
define the declarations used to judge itself.
S2 must independently validate approved additions before extending that baseline.

`extract_sources` accepts actual project sources. `extract_components` also has
`project_sources` for helper/base sources outside a micro writer. Project
inheritance is linked explicitly; it is not a union of members from all variants.
An unresolved external type is a visible missing-reference boundary, not an
inferred framework API. Framework members/assembly references remain a separate
compilation concern.

`tools/extract_component_api.py --selection selection.json --output out/api`
accepts an array of existing Subsystem objects, not a new pipeline schema.
S2 must reuse #279's `meta.compose_selection` record for selection and replay.

## Format and toolchain

`unity-component-api/v1` records ordered fixed selections, actual class/variant,
fixed-schema identity, logical source paths and exact UTF-8 content hashes; extractor
source fingerprint, pinned SDK 9.0.312, Roslyn version, C# 9 and explicit defines;
qualified/nested project types, generic arity and base/interface definition links;
member kind, declaring identity, accessibility, static/instance status, declared
type, overloads, generic arity, ref/parameter modifiers, defaults, optional/params
arguments, event signatures and accessor visibility. Public synthesized members
are marked. Private declarations remain available for negative diagnostics.

SDK roll-forward is disabled. Python delegates C# declaration parsing to SDK
Roslyn. Core runtime references resolve System delegate signatures, while Unity
binding remains unverified. Normalization sorts sources, symbols and JSON keys;
logical paths exclude temporary filesystem locations. Hash derivation uses the
shared `src.ident` substrate with registered `component.api` and
`component.api.source` domains; no ad-hoc digest implementation is introduced. There is no manifest cache
keyed only by class/variant. Tool binaries and intermediate directories are
keyed by extractor source fingerprint and the pinned SDK.

Unavailable SDK/build/invocation and malformed C# raise explicit exceptions.
Evidence directories preserve failed sources and structured diagnostic JSON.
Skipped extraction is not acceptance. The generated-C# job installs the exact
SDK and runs with `REQUIRE_COMPONENT_API=1`, forbidding missing-SDK skips.

## Evidence and boundaries

Engineer-authored fixtures cover health/dialogue HUDs; renamed/dynamic widget
exports; slotted/plain saves and generated Data helpers; ordinary, pointer and
multi-mode controllers; dynamic animator names; project inheritance, generic
methods, overloads, modifiers, accessors, events, optional/params arguments;
repeatability, malformed-source retention and unavailable tooling.

The permanent mutation gate renames a real HUD `SetHealth` export in a copied
template, without editing a member list. Extraction removes the old export,
adds the renamed export and changes identity. Restoring the template restores
the manifest exactly. A shadow fixture demonstrates the old union table's
false acceptance of `ShowLine` on a health-only HUD. S1 does not switch production
consumers; S2 must extend mutation coverage to prompts, wiring and validation.

`tools/component_api_fixtures.py` renders eleven selected configurations into
separate namespaces within the real writer-reported CI project root. The Unity
Editor job compiles all configurations, including actual input/vehicle
interfaces, source-only sprite rendering, nested FSM handling and driven animator
parameters. Regression probes preserve original input, retain the driven setter
shape, prevent public declarations escaping nested fragments, and forbid asset
backend calls even when extraction tooling is unavailable. The fixture components are not attached to the captured game scene.
Only a successful Editor job establishes semantic compilation. Syntax parsing
and a tool build do not prove overload/type binding, runtime behavior or LLM
fidelity. Existing Unity version, action pins and credential handling are kept.

## Compatibility and rollback

Production class identities, GUIDs, scene file IDs, weapon/firing/movement/pool,
board/rules behavior, saved schema formats, intent ledger/resolution v1 and the
Step-3.5 LLM freeze remain unchanged. Current consumers/sanitizers and all three
copied tables stay authoritative in S1. Rollback reverts the extraction tool,
shadow adapter, tests and CI fixture as a coherent slice. S2 requires a separate
contract/consumer rollback; no second runtime owner is activated for comparison.
