"""Model / visual representation atoms — mesh, material, LOD, and animation state.

These are the primitive opcodes for the visual layer:
load a model, assign a material, force an LOD level, set an Animator parameter.

All use Resources.Load paths (relative to a Resources/ folder) or component names.
"""

from .base import AtomDef, AtomParam

MODEL_ATOMS: list[AtomDef] = [
    AtomDef(
        name="SetModel",
        category="model",
        description="Swap the entity's mesh to a prefab/model loaded from Resources.",
        duration="instant",
        side_effects=["mesh", "renderer"],
        aliases=["set_model", "load_model", "swap_model", "assign_model"],
        engines=["unity"],
        params=[
            AtomParam(name="model_path", type="string", default="Models/Default", required=True,
                      description="Resources-relative path to the model prefab or mesh asset"),
        ],
    ),
    AtomDef(
        name="SetMaterial",
        category="model",
        description="Assign a named material from Resources to the entity's MeshRenderer slot.",
        duration="instant",
        side_effects=["material"],
        aliases=["set_material", "apply_material", "assign_material", "swap_material"],
        engines=["unity"],
        params=[
            AtomParam(name="material_name", type="string", default="Materials/Default", required=True,
                      description="Resources-relative path to the material asset"),
            AtomParam(name="slot",          type="int",    default=0,
                      description="Material slot index on the MeshRenderer (0 = first)"),
        ],
    ),
    AtomDef(
        name="SetLOD",
        category="model",
        description="Force a specific LOD level on the entity's LODGroup component.",
        duration="instant",
        side_effects=["lod"],
        aliases=["set_lod", "force_lod", "lod_level"],
        engines=["unity"],
        params=[
            AtomParam(name="level", type="int", default=0,
                      description="LOD level to force (0 = highest detail; -1 = auto)"),
        ],
    ),
    AtomDef(
        name="SetAnimationState",
        category="model",
        description="Set an Animator parameter (bool, int, float, or trigger) by name.",
        duration="instant",
        side_effects=["animation"],
        aliases=["set_animation_state", "animator_param", "set_anim_param", "anim_state"],
        engines=["unity"],
        params=[
            AtomParam(name="param",      type="string", default="Idle",   required=True,
                      description="Animator parameter name"),
            AtomParam(name="value",      type="string", default="true",
                      description="Value to set (as string; for trigger leave empty or 'true')"),
            AtomParam(name="param_type", type="string", default="trigger",
                      description="Parameter type: bool | int | float | trigger"),
        ],
    ),
]
