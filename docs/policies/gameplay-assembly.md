# Gameplay assembly policy

Compose Micros is the only active gameplay assembly mechanism for new
`unity-create` runs and is enabled by default. Mechanics enter through
`MechanicRegistry`; new keys must not be added to
`_LEGACY_SCRIPT_TEMPLATES`.

The legacy template map remains read-only for deterministic replay of older
captured schemas through `unity-create --legacy-templates` or a non-compose
`unity-validate` replay. It is a
compatibility surface, not a second extension point.
