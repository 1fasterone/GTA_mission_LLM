#!/usr/bin/env python3
"""
Prove the MANAGER's three-tier reuse decision against real (headless) Blender:
a cold build caches the pre-rig mesh; a colour variant (different full fingerprint
→ skip-work miss) must geo-reuse it and run only the material/rig/export delta.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from src.blender import BlenderJobManager, build_job


def main() -> int:
    out = Path("output/blender/_verify_mgr").resolve()
    if out.exists():
        shutil.rmtree(out)
    cfg = {"output_dir": str(out)}

    red = build_job(name="mgr_hero", asset_type="character", style="low_poly")
    red.rig = True; red.animations = ["walk"]; red.detail = 3; red.seed = 7; red.color = "#FF0000"

    green = build_job(name="mgr_hero", asset_type="character", style="low_poly")
    green.rig = True; green.animations = ["walk"]; green.detail = 3; green.seed = 7; green.color = "#00FF00"

    m = BlenderJobManager(cfg)
    print("blender:", m.blender.path if m.blender else None, "| blender_version:", m.cache.blender_version)
    geo_fp = m.cache.geo_fingerprint(red)
    print("geo_fp(red) == geo_fp(green):", geo_fp == m.cache.geo_fingerprint(green))
    print("full fp(red) != full fp(green):",
          m.cache.fingerprint(red, ["generate", "rig", "export"]) != m.cache.fingerprint(green, ["generate", "rig", "export"]))

    # cold build (red) — caches the pre-rig mesh
    m.submit(red)
    r1 = m.run_all().results[0]
    print("\nCOLD red :", r1.status.value, "| geo_hash", (r1.manifest.geo_hash if r1.manifest else None))
    print("   ops:", r1.manifest.operations if r1.manifest else None)
    print("   mesh cached:", m.cache.geo.get_mesh(geo_fp) is not None)

    # variant (green) — fresh manager sharing the cache → must reuse geometry
    m2 = BlenderJobManager(cfg)
    m2.submit(green)
    r2 = m2.run_all().results[0]
    print("\nVARIANT green:", r2.status.value, "| geo_hash", (r2.manifest.geo_hash if r2.manifest else None))
    print("   ops:", r2.manifest.operations if r2.manifest else None)

    ok = (r1.manifest and r2.manifest
          and r1.manifest.geo_hash == r2.manifest.geo_hash
          and "geo_reuse" in r2.manifest.operations
          and "generate:humanoid_base" not in r2.manifest.operations)
    print("\nRESULT:", "PASS — variant reused cold geometry, ran only the delta" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
