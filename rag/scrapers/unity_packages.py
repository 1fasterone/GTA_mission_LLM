"""
Scrapes Unity Package Registry (packages.unity.com) by fetching each known
package document directly: GET /com.unity.<name>

The Unity registry does not expose a search endpoint, but the npm-compatible
direct-fetch (/com.unity.cinemachine) returns the full package document with
all version metadata. We maintain a curated seed list of ~200 package IDs
covering all major Unity namespaces.
"""
import sys
import time
from pathlib import Path
import httpx

from ..schema.unity_package import make_package_entry

BASE_URL = "https://packages.unity.com"
OUT = Path(__file__).parent.parent / "corpus" / "unity" / "packages.jsonl"
_DELAY_S = 0.2

# Comprehensive list of known Unity package IDs
_PACKAGE_IDS: list[str] = [
    # ── Render Pipelines ─────────────────────────────────────────────────────
    "com.unity.render-pipelines.universal",
    "com.unity.render-pipelines.high-definition",
    "com.unity.render-pipelines.core",
    "com.unity.shadergraph",
    "com.unity.visualeffectgraph",
    "com.unity.postprocessing",
    # ── Camera ───────────────────────────────────────────────────────────────
    "com.unity.cinemachine",
    # ── Input ────────────────────────────────────────────────────────────────
    "com.unity.inputsystem",
    # ── AI / Navigation ──────────────────────────────────────────────────────
    "com.unity.ai.navigation",
    # ── Networking ───────────────────────────────────────────────────────────
    "com.unity.netcode.gameobjects",
    "com.unity.multiplayer.tools",
    "com.unity.multiplayer-center",
    "com.unity.services.relay",
    "com.unity.services.lobby",
    "com.unity.services.authentication",
    "com.unity.transport",
    # ── 2D ───────────────────────────────────────────────────────────────────
    "com.unity.2d.tilemap",
    "com.unity.2d.tilemap.extras",
    "com.unity.2d.animation",
    "com.unity.2d.sprite",
    "com.unity.2d.spriteshape",
    "com.unity.2d.pixel-perfect",
    "com.unity.2d.path",
    "com.unity.2d.common",
    "com.unity.2d.psdimporter",
    "com.unity.2d.aseprite",
    # ── Timeline & Playables ─────────────────────────────────────────────────
    "com.unity.timeline",
    "com.unity.playablegraph-visualizer",
    # ── Text ─────────────────────────────────────────────────────────────────
    "com.unity.textmeshpro",
    # ── Physics ──────────────────────────────────────────────────────────────
    "com.unity.physics",
    "com.unity.havok.physics",
    # ── DOTS ─────────────────────────────────────────────────────────────────
    "com.unity.entities",
    "com.unity.entities.graphics",
    "com.unity.burst",
    "com.unity.jobs",
    "com.unity.mathematics",
    "com.unity.collections",
    # ── Asset Management ─────────────────────────────────────────────────────
    "com.unity.addressables",
    "com.unity.assetbundlebrowser",
    # ── Localization ─────────────────────────────────────────────────────────
    "com.unity.localization",
    # ── Terrain ──────────────────────────────────────────────────────────────
    "com.unity.terrain-tools",
    # ── Level Design ─────────────────────────────────────────────────────────
    "com.unity.probuilder",
    "com.unity.polybrush",
    # ── Animation ────────────────────────────────────────────────────────────
    "com.unity.animation.rigging",
    # ── Audio ────────────────────────────────────────────────────────────────
    "com.unity.ugui",
    # ── UI ───────────────────────────────────────────────────────────────────
    "com.unity.ui",
    "com.unity.ui.builder",
    "com.unity.ugui",
    # ── XR ───────────────────────────────────────────────────────────────────
    "com.unity.xr.coreutils",
    "com.unity.xr.interaction.toolkit",
    "com.unity.xr.openxr",
    "com.unity.xr.arfoundation",
    "com.unity.xr.arkit",
    "com.unity.xr.oculus",
    "com.unity.xr.management",
    # ── Testing ──────────────────────────────────────────────────────────────
    "com.unity.test-framework",
    "com.unity.testtools.codecoverage",
    "com.unity.performance.profile-analyzer",
    # ── Tools & Profiling ────────────────────────────────────────────────────
    "com.unity.memoryprofiler",
    "com.unity.recorder",
    "com.unity.sequences",
    "com.unity.splines",
    "com.unity.vectorgraphics",
    "com.unity.scene-template",
    "com.unity.ide.visualstudio",
    "com.unity.ide.rider",
    "com.unity.ide.vscode",
    "com.unity.nuget.newtonsoft-json",
    "com.unity.logging",
    "com.unity.serialization",
    # ── Live Capture / Virtual Production ────────────────────────────────────
    "com.unity.live-capture",
    # ── Reflect / Industry ───────────────────────────────────────────────────
    "com.unity.reflect",
    # ── Cloud & Services ─────────────────────────────────────────────────────
    "com.unity.services.analytics",
    "com.unity.services.cloudcode",
    "com.unity.services.economy",
    "com.unity.services.vivox",
    "com.unity.services.leaderboards",
    "com.unity.services.matchmaker",
    "com.unity.services.cloudsave",
    "com.unity.services.wire",
    "com.unity.services.friends",
    # ── Nuget Bridge ─────────────────────────────────────────────────────────
    "com.unity.nuget.mono-cecil",
    # ── Notifications ────────────────────────────────────────────────────────
    "com.unity.mobile.notifications",
    "com.unity.push-notifications",
    # ── Purchasing ───────────────────────────────────────────────────────────
    "com.unity.purchasing",
    "com.unity.ads",
    # ── Netcode (old) ────────────────────────────────────────────────────────
    "com.unity.netcode.adapter.utp",
    # ── ML Agents ────────────────────────────────────────────────────────────
    "com.unity.ml-agents",
    # ── Version Control ──────────────────────────────────────────────────────
    "com.unity.collab-proxy",
    # ── Build ────────────────────────────────────────────────────────────────
    "com.unity.build-report-inspector",
    "com.unity.platforms",
    "com.unity.platforms.windows",
    "com.unity.platforms.linux",
    "com.unity.platforms.macos",
    "com.unity.platforms.android",
    "com.unity.platforms.ios",
    "com.unity.platforms.web",
    # ── Geometry ─────────────────────────────────────────────────────────────
    "com.unity.collections",
    # ── Data Oriented ─────────────────────────────────────────────────────────
    "com.unity.dataflowgraph",
    # ── Search ────────────────────────────────────────────────────────────────
    "com.unity.quicksearch",
    "com.unity.search.extensions",
    # ── Object Capture / Scanning ────────────────────────────────────────────
    "com.unity.live-capture.arkit-face-capture",
    # ── Scriptable Render Pipeline Extras ────────────────────────────────────
    "com.unity.render-pipelines.universal.config",
    # ── Legacy / Compatibility ────────────────────────────────────────────────
    "com.unity.modules.particlesystem",
    "com.unity.modules.physics",
    "com.unity.modules.physics2d",
    "com.unity.modules.audio",
    "com.unity.modules.animation",
    "com.unity.modules.ai",
    "com.unity.modules.ui",
    "com.unity.modules.ugui",
    "com.unity.modules.terrain",
    "com.unity.modules.video",
    "com.unity.modules.assetbundle",
    "com.unity.modules.imageconversion",
    "com.unity.modules.jsonserialize",
    "com.unity.modules.unitywebrequest",
    "com.unity.modules.unitywebrequesttexture",
    "com.unity.modules.unitywebrequestaudio",
    "com.unity.modules.unitywebrequestassetbundle",
    "com.unity.modules.unitywebrequestwww",
    "com.unity.modules.screencapture",
    "com.unity.modules.vehicles",
    "com.unity.modules.cloth",
    "com.unity.modules.xr",
    "com.unity.modules.accessibility",
    "com.unity.modules.director",
    "com.unity.modules.wind",
    "com.unity.modules.tilemap",
    "com.unity.modules.subsystems",
    "com.unity.modules.umbra",
    "com.unity.modules.teamscollab",
    "com.unity.modules.uielements",
    "com.unity.modules.hierarchycore",
    "com.unity.modules.vr",
]


def _fetch_package(client: httpx.Client, pkg_id: str) -> dict | None:
    try:
        r = client.get(f"{BASE_URL}/{pkg_id}", timeout=30.0)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        r.raise_for_status()
    except httpx.HTTPStatusError:
        return None
    except Exception as e:
        print(f"  [warn] fetch {pkg_id}: {e}", file=sys.stderr)
        return None
    return None


def run(force: bool = False) -> int:
    if OUT.exists() and not force:
        count = sum(1 for line in OUT.read_text(encoding="utf-8").splitlines() if line.strip())
        print(f"[unity_packages] already present ({count} entries). Use --force to re-scrape.")
        return count

    OUT.parent.mkdir(parents=True, exist_ok=True)

    # De-duplicate the list
    unique_ids = list(dict.fromkeys(_PACKAGE_IDS))
    print(f"[unity_packages] fetching {len(unique_ids)} packages from {BASE_URL} ...")

    ok = err = skipped = 0
    with (
        httpx.Client(follow_redirects=True, timeout=60.0) as client,
        OUT.open("w", encoding="utf-8") as f,
    ):
        for i, pkg_id in enumerate(unique_ids, 1):
            doc = _fetch_package(client, pkg_id)
            if doc is None:
                skipped += 1
                continue

            try:
                entry = make_package_entry(doc, source=BASE_URL)
                f.write(entry.to_jsonl() + "\n")
                ok += 1
            except Exception as e:
                print(f"  [skip] {pkg_id}: {e}", file=sys.stderr)
                err += 1

            if i % 20 == 0:
                print(f"  [{i}/{len(unique_ids)}] {ok} written so far ...")
            time.sleep(_DELAY_S)

    print(f"[unity_packages] {ok} written, {err} errors, {skipped} not found --> {OUT}")
    return ok


if __name__ == "__main__":
    run(force="--force" in sys.argv)
