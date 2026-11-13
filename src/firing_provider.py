"""The one firing provider contract shared by fire input and the Shoot atom (R3, S5).

A shooting controller (Controller.cs.j2 with the "shoot" ability) implements
``IFiringProvider``. Its ``Update`` fire input and the Unity ``Shoot`` atom
lowerer both reach the same ``TryFire()`` gate, so ammunition, fire-rate
cadence and reload state have exactly one owner, and a refused shot names its
reason as a ``FireResult`` instead of failing silently.

This module holds only the names both sides agree on. It imports nothing from
the atom backends or the micro generators, so each can import it without
depending on the other:

- ``src/atoms/backends/unity.py`` lowers ``Shoot`` to ``RESOLVER_CALL``;
- ``src/micro/controller.py`` emits ``TEMPLATE`` beside the controller;
- ``src/atom_bindings.py`` reads ``RESOLVER_CALL`` back out of the lowered C#
  to record which runtime contract a lowered atom depends on;
- ``src/unity_generator.py`` emits a contract-only copy when a lowered atom
  needs the contract and no composed controller implements it.
"""
from __future__ import annotations

CONTRACT = "IFiringProvider"
# The scene object the lowered C# resolves the provider on (its tag).
HOST = "Player"
# The one call the Shoot lowerer emits; FiringProvider.cs declares it.
RESOLVER_CALL = "FiringProviders.FirePlayer()"
# The template, relative to the controller micro's own template folder.
TEMPLATE = "FiringProvider.cs.j2"
# The same template, relative to templates/unity (the project generator's root).
PROJECT_TEMPLATE = "micro/controller/FiringProvider.cs.j2"
SCRIPT = "FiringProvider.cs"
# Named when a lowered atom needs the contract and nothing implements it.
REMEDY = ("compose a controller whose variant renders Controller.cs.j2 with the "
          "shoot ability")
