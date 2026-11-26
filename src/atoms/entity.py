"""Entity atoms — create, destroy, or mutate game entities.

``entity`` params reserve one token: ``self`` (any case) is the GameObject that
runs the sequence, the spawned instance an entity behavior is attached to. A
spawned instance is named ``<group>_<i>``, so its own group name does not find
it. Unity lowers ``self`` to ``gameObject`` (src/atoms/backends/unity.py).
"""

from .base import AtomDef, AtomParam

ENTITY_ATOMS: list[AtomDef] = [
    AtomDef(
        name="SpawnEntity",
        category="entity",
        description="Instantiate an entity prefab at a world position.",
        duration="instant",
        side_effects=["scene_hierarchy"],
        aliases=["spawn_entity", "spawn", "create_entity", "instantiate", "spawn_prefab"],
        params=[
            AtomParam(name="entity_type", type="entity_ref", required=True,
                      description="Prefab key / entity type (e.g. 'Enemy', 'Coin')"),
            AtomParam(name="x", type="float", default=0.0),
            AtomParam(name="y", type="float", default=0.0),
            AtomParam(name="z", type="float", default=0.0),
        ],
    ),
    AtomDef(
        name="Despawn",
        category="entity",
        description="Destroy a named entity in the scene.",
        duration="instant",
        side_effects=["scene_hierarchy"],
        aliases=["despawn", "destroy", "remove_entity", "kill_entity"],
        params=[
            AtomParam(name="entity", type="entity_ref", required=True,
                      description="Name of the scene entity to destroy, or "
                                  "'self' for the GameObject running the sequence"),
        ],
    ),
    AtomDef(
        name="SetHealth",
        category="entity",
        description="Set a named entity's HealthSystem value directly.",
        duration="instant",
        side_effects=["health"],
        aliases=["set_health", "heal", "damage", "set_hp"],
        params=[
            AtomParam(name="entity", type="entity_ref", required=True,
                      description="Entity name, 'Player', or 'self' for the "
                                  "GameObject running the sequence"),
            AtomParam(name="value",  type="float", required=True,
                      description="Absolute health value to set"),
        ],
    ),
    AtomDef(
        name="EquipWeapon",
        category="entity",
        description="Give a weapon item to a named entity's Inventory component.",
        duration="instant",
        side_effects=["inventory"],
        aliases=["equip_weapon", "give_weapon", "arm", "give_item"],
        params=[
            AtomParam(name="entity", type="entity_ref", required=True,
                      description="Entity to equip, 'Player', or 'self' for "
                                  "the GameObject running the sequence"),
            AtomParam(name="weapon", type="string",    required=True,
                      description="Weapon identifier key"),
            AtomParam(name="ammo", type="int", default=100,
                      description="Ammo to give with the weapon (GTA5 only)"),
        ],
    ),
]
