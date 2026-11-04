"""Single deterministic spatial primitive selection pathway."""

from __future__ import annotations


GROUND_PLANE = "ground_plane"
ROUTE_CORRIDOR = "route_corridor"
AIRSPACE_COLUMN = "airspace_column"
DEPTH_COLUMN = "depth_column"

_BY_LOCOMOTION = {"fly": AIRSPACE_COLUMN, "swim": DEPTH_COLUMN}


class PrimitiveProvider:
    """Owns primitive selection; callers do not chain selectors."""

    @staticmethod
    def select(game_type: str, locomotion: str, *, has_route: bool = False) -> str:
        del game_type  # reserved for future closed mappings
        if has_route:
            return ROUTE_CORRIDOR
        return _BY_LOCOMOTION.get((locomotion or "").lower(), GROUND_PLANE)


def select_primitive(game_type: str, locomotion: str,
                     has_route: bool = False) -> str:
    return PrimitiveProvider.select(game_type, locomotion, has_route=has_route)
