"""The one firing provider contract shared by player input and the weapon atoms.

R3 (S5) introduced it for ``Shoot``; R4 (S6) routes ``Reload`` through it too.
A shooting controller (Controller.cs.j2 with the "shoot" ability) implements
``IFiringProvider``. Its ``Update`` fire input and the Unity ``Shoot`` lowerer
both reach the same ``TryFire()`` gate, and its reload input and the ``Reload``
lowerer both reach the same ``TryReload()`` gate. Ammunition, fire-rate cadence
and reload state therefore have exactly one owner, and a refused call names its
reason (a ``FireResult`` or ``ReloadResult``) instead of failing silently.

This module holds only the names both sides agree on. It imports nothing from
the atom backends or the micro generators, so each can import it without
depending on the other:

- ``src/atoms/backends/unity.py`` lowers each atom in ``ATOM_CALLS`` to its call;
- ``src/micro/controller.py`` emits ``TEMPLATE`` beside the controller;
- ``src/atom_bindings.py`` reads those calls back out of the lowered C# to
  record which runtime contract a lowered atom depends on;
- ``src/unity_generator.py`` emits a contract-only copy when a lowered atom
  needs the contract and no composed controller implements it.
"""
from __future__ import annotations

CONTRACT = "IFiringProvider"
# The scene object the lowered C# resolves the provider on (its tag).
HOST = "Player"
# The composed-unit target scene.unity.j2 attaches to that object
# (`player_units`). A shooting controller on any other target implements the
# contract where the resolver never looks, so it is not the implementer.
HOST_TARGET = "player"
# The call the Shoot lowerer emits; FiringProvider.cs declares it.
RESOLVER_CALL = "FiringProviders.FirePlayer()"
# The call the Reload lowerer emits; FiringProvider.cs declares it.
RELOAD_CALL = "FiringProviders.ReloadPlayer()"
# Each atom lowered through the provider, with the one call it emits and the
# controller gate that call reaches. Add an atom here only together with its
# lowerer, its gate in Controller.cs.j2 and its ownership-audit route.
ATOM_CALLS: dict[str, str] = {
    "Shoot": RESOLVER_CALL,
    "Reload": RELOAD_CALL,
}
ATOM_GATES: dict[str, str] = {
    "Shoot": "TryFire",
    "Reload": "TryReload",
}
# The template, relative to the controller micro's own template folder.
TEMPLATE = "FiringProvider.cs.j2"
# The same template, relative to templates/unity (the project generator's root).
PROJECT_TEMPLATE = "micro/controller/FiringProvider.cs.j2"
SCRIPT = "FiringProvider.cs"
# Named when a lowered atom needs the contract and nothing implements it.
REMEDY = ("compose a controller on the player target whose variant renders "
          "Controller.cs.j2 with the shoot ability")
