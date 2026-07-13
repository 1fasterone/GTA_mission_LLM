"""Engine backend translators — atom IR → engine-specific code snippets."""
from .unity import unity_atom_snippet, unity_coroutine_body
from .gta5  import gta5_atom_snippet, gta5_tick_body

_ENGINE_SNIPPETS = {
    "unity": unity_atom_snippet,
    "gta5":  gta5_atom_snippet,
}


def atom_snippet(engine: str, atom: str, args: dict | None = None) -> str:
    """Engine-neutral dispatcher: render `atom` for `engine`.

    Raises ValueError for an unknown engine, an unknown atom, or an atom whose
    AtomDef.engines doesn't include the requested engine (FAIL-FAST — never a
    comment stub).
    """
    fn = _ENGINE_SNIPPETS.get(engine)
    if fn is None:
        raise ValueError(f"unknown engine {engine!r} — expected one of "
                         f"{sorted(_ENGINE_SNIPPETS)}")
    # Local import: src.atoms.__init__ does not import backends at module
    # level, but keep this lazy so that never becomes a cycle.
    from .. import get_atom
    adef = get_atom(atom)
    if adef is None:
        raise ValueError(f"unknown atom {atom!r}")
    if engine not in adef.engines:
        raise ValueError(f"atom {adef.name!r} is not available on engine "
                         f"{engine!r} (engines={adef.engines})")
    return fn(adef.name, args)


__all__ = [
    "unity_atom_snippet", "unity_coroutine_body",
    "gta5_atom_snippet", "gta5_tick_body",
    "atom_snippet",
]
