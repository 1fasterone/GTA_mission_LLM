"""One facade for every supported visual asset-binding input."""

from __future__ import annotations

from typing import Any


class AssetBinder:
    def bind_records(self, schema: Any,
                     records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        from .unity_visual_bindings import bind_visual_assets

        return bind_visual_assets(schema, records)

    def bind_closed_plan(self, schema: Any, plan: dict[str, Any]) -> dict[str, Any]:
        from .visual_reasoning_compile import bind_visual_assets

        return bind_visual_assets(schema, plan)
