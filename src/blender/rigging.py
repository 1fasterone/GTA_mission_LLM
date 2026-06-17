"""
Rig strategy router (Micro-Generator Phase 3 — geometry reuse).

We always cache the **pre-rig mesh** (clean verts + UVs, no armature), so rigging
is a deterministic *per-job* stage that runs after geometry reuse. "Rig a crate"
and "rig a humanoid" are different problems, so this is a deterministic router —
the same shape as `job_builder.route_for` (procedural-vs-diffusion) — that maps a
job to one of a small set of **versioned rig micros**.

Two decisions are encoded here:

  1. **Template classes** — the authored-armature archetypes:
         humanoid · quadruped · car · rifle · door · crate
     Each maps to a ``RigTemplate<Class>V1`` micro that injects a pre-authored
     armature from a shipped ``.blend`` library.

  2. **Rig-micro naming + versioning** — every strategy has a stable, versioned id
     so it can be folded into the cache fingerprint (bump the version when the rig
     code changes and the cache busts correctly):
         RigTemplateHumanoidV1 · RigTemplateCarV1 · …            (template family)
         RigAutoStaticV1 · RigAutoMechanicalV1 · RigAutoHumanoidV1 · RigAutoRagdollV1
                                                                  (auto family)

Graceful degradation (the package's signature): a template class with no authored
armature available **falls back** to the nearest deterministic auto micro — so the
pipeline rigs *something* today (auto everywhere) and authored templates slot in
later for quality without a rearchitecture. The same way: no Blender → stub, no
diffusion → procedural, **no template → auto-rig**.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

# Decision 1: the authored-armature archetypes.
TEMPLATE_CLASSES: tuple[str, ...] = ("humanoid", "quadruped", "car", "rifle", "door", "crate")

# Version stamped into every micro id; bump when a family's rig code changes.
RIG_VERSION = 1


class RigKind(str, Enum):
    NONE       = "none"        # static export, no armature
    MECHANICAL = "mechanical"  # procedural named bones (wheels, hinges, slides)
    AUTO       = "auto"        # auto-skeleton (Rigify metarig fit)
    RAGDOLL    = "ragdoll"     # physics ragdoll auto-rig
    TEMPLATE   = "template"    # injected pre-authored armature


@dataclass(frozen=True)
class RigStrategy:
    """One resolved rig decision. `id` is the versioned micro name folded into the
    cache fingerprint; `kind` selects the emitted-script behaviour."""
    id:             str
    kind:           RigKind
    template_class: Optional[str] = None   # set for TEMPLATE kind
    armature:       Optional[str] = None   # template .blend the script appends

    @property
    def needs_armature(self) -> bool:
        """Whether this strategy actually adds a skeleton (drives whether the
        manager includes the `rig` step at all)."""
        return self.kind is not RigKind.NONE


# Static singletons for the auto family (no per-job state).
_AUTO_STATIC     = RigStrategy(f"RigAutoStaticV{RIG_VERSION}",     RigKind.NONE)
_AUTO_MECHANICAL = RigStrategy(f"RigAutoMechanicalV{RIG_VERSION}", RigKind.MECHANICAL)
_AUTO_HUMANOID   = RigStrategy(f"RigAutoHumanoidV{RIG_VERSION}",   RigKind.AUTO)
_AUTO_RAGDOLL    = RigStrategy(f"RigAutoRagdollV{RIG_VERSION}",    RigKind.RAGDOLL)

# asset_type → template class. Anything absent (terrain/foliage/environment/
# material) has no meaningful skeleton → static.
_CLASS_FOR_ASSET: dict[str, str] = {
    "character": "humanoid",
    "creature":  "quadruped",
    "vehicle":   "car",
    "weapon":    "rifle",
    "building":  "door",
    "prop":      "crate",
}

# Explicit geometry_nodes recipe names route straight to a class.
_CLASS_FOR_GEO: dict[str, str] = {
    "human_base": "humanoid", "humanoid_base": "humanoid",
    "creature_base": "quadruped", "quadruped": "quadruped",
    "vehicle_base": "car", "rifle": "rifle", "door": "door", "crate": "crate",
}

# Fallback auto micro per template class when no authored armature is available.
_FALLBACK: dict[str, RigStrategy] = {
    "humanoid":  _AUTO_HUMANOID,
    "quadruped": _AUTO_HUMANOID,    # Rigify human metarig (rough); V1 caveat
    "car":       _AUTO_MECHANICAL,
    "door":      _AUTO_MECHANICAL,
    "rifle":     _AUTO_MECHANICAL,
    "crate":     _AUTO_STATIC,      # a crate doesn't deform
}


def rig_class_for(asset_type: str, geometry_nodes: str = "") -> Optional[str]:
    """Map a job's asset_type / geometry_nodes to a template class, or None for
    asset types with no meaningful skeleton."""
    if geometry_nodes and geometry_nodes in _CLASS_FOR_GEO:
        return _CLASS_FOR_GEO[geometry_nodes]
    return _CLASS_FOR_ASSET.get(asset_type)


def template_available(template_class: str, template_root: Optional[str]) -> bool:
    """True when an authored armature `.blend` for `template_class` ships in
    `template_root`. With no library configured (today's state) this is always
    False → every template request falls back to a deterministic auto micro."""
    if not template_root:
        return False
    return (Path(template_root) / f"{template_class}.blend").is_file()


def rig_strategy(job, template_root: Optional[str] = None) -> RigStrategy:
    """Deterministically resolve a job to a versioned rig micro.

    Order: no rig requested → static; explicit ragdoll hint → ragdoll; otherwise
    classify by asset_type/geometry_nodes and prefer an authored template, else
    fall back to the nearest deterministic auto micro.
    """
    if not getattr(job, "rig", False):
        return _AUTO_STATIC

    meta = getattr(job, "meta", {}) or {}
    if str(meta.get("rig_kind", "")).lower() == "ragdoll":
        return _AUTO_RAGDOLL

    cls = rig_class_for(getattr(job, "asset_type", ""), getattr(job, "geometry_nodes", ""))
    if cls is None:
        return _AUTO_STATIC

    if cls in TEMPLATE_CLASSES and template_available(cls, template_root):
        return RigStrategy(
            id=f"RigTemplate{cls.capitalize()}V{RIG_VERSION}",
            kind=RigKind.TEMPLATE,
            template_class=cls,
            armature=f"{cls}.blend",
        )
    return _FALLBACK.get(cls, _AUTO_STATIC)
