"""
Inventory of every `hashlib` digest outside src/ident/ — and why each exists.

src/ident/ is the one identity substrate (docs/SCENE_GRAPH_COMPILER.md, Phase
0). The digests below predate it. Each one is listed per enclosing scope with
its exact call count and one of two dispositions:

  keep     must NOT be unified onto src.ident. Three reasons qualify:
             contract — an external or already-shipped format depends on the
               exact digest (the npm registry's sha1; Unity .meta GUIDs every
               generated project already carries);
             output seed — the digest seeds generation, so changing it changes
               every generated cue / sprite / mesh / terrain / asset pick;
             wrapped — Track A adapts it rather than replacing it (the
               BlenderCache fingerprints), or it is a content-derived local
               cache key that is already machine-stable.
  migrate  retired onto src.ident by the named plan slice. None are open:
           A4 retired the two that hashed an ABSOLUTE path.

src/ident/lint.py holds this list to the real tree exactly: a new digest
anywhere in src/ or cli/ fails the lint until it goes through src.ident or
earns a row here, and a row left behind after a migration fails it too.

Deliberately NOT listed: the md5 inside the script src/blender/mcp_backend.py
emits. It is a string executed by Blender's own interpreter, which cannot
import src.ident; it is not a hashlib call in this process.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Disposition = Literal["keep", "migrate"]


@dataclass(frozen=True)
class DigestSite:
    path: str                 # repo-relative, forward slashes
    scope: str                # enclosing qualname, "<module>" at top level
    calls: int                # hashlib constructor calls in that scope
    disposition: Disposition
    reason: str
    slice: str = ""           # migrate only: the retiring slice in the plan doc

    def __post_init__(self) -> None:
        if self.calls < 1:
            raise ValueError(f"{self.path} {self.scope}: calls must be >= 1")
        if not self.reason.strip():
            raise ValueError(f"{self.path} {self.scope}: a row must say why")
        if (self.disposition == "migrate") != bool(self.slice):
            raise ValueError(
                f"{self.path} {self.scope}: 'migrate' rows name their retiring "
                "slice and 'keep' rows name none")


SITES: tuple[DigestSite, ...] = (
    # ── contracts ─────────────────────────────────────────────────────────────
    DigestSite("src/unity_generator.py", "_make_guid", 1, "keep",
               "contract: deterministic Unity .meta GUIDs that every already-"
               "generated project carries; pinned as GUID_SCHEME = 1"),
    DigestSite("src/unity_generator.py", "_make_uuid", 1, "keep",
               "contract: deterministic ids baked into the generated .inputactions "
               "asset; same scheme as _make_guid"),
    DigestSite("src/unity_package_cache.py", "download", 1, "keep",
               "contract: verifies a tarball against the npm registry's own "
               "sha1 `shasum` field — the registry chooses the algorithm"),
    DigestSite("src/visual_reasoning_compiler.py", "sha256_bytes", 1, "keep",
               "contract: exact SHA-256 hashes recorded for visual-board, "
               "Qwen-output, and provenance artifacts"),
    DigestSite("src/unity_entity_asset_resolver.py", "resolve_entity_assets", 1,
               "keep",
               "contract: exact SHA-256 verifies a locked visual asset binding "
               "against its fulfilment manifest"),
    DigestSite("src/unity_generator.py", "_frozen_asset_selection", 1, "keep",
               "contract: exact SHA-256 binds a saved Sketchfab selection to "
               "the cached model bytes used by replay"),
    DigestSite("src/unity_sketchfab_collection.py", "_sha256_file", 1, "keep",
               "contract: exact SHA-256 verifies the cached Sketchfab bytes "
               "recorded in the frozen collection lock"),
    DigestSite("src/unity_greybox.py", "_canonical_sha256", 1, "keep",
               "contract: Greybox+ source_greybox_sha256 binds the canonical "
               "dungeon IR embedded in its sidecar"),
    DigestSite("src/unity_greybox.py", "stamp_greybox_plus_reference", 2,
               "keep",
               "contract: raw SHA-256 provenance binds the exact reference "
               "image bytes and visual-context text accepted before Step 4"),
    DigestSite("src/unity_run_log.py", "_read_raw_input", 2, "keep",
               "contract: exact SHA-256 records the raw prompt bytes in the "
               "unity-create-run-log/v1 manifest and names its artifact pair"),

    # ── output seeds ──────────────────────────────────────────────────────────
    DigestSite("src/audio/content.py", "_seed_for", 1, "keep",
               "output seed: per-cue audio generation seed"),
    DigestSite("src/audio/dialog_content.py", "_seed_for", 1, "keep",
               "output seed: per-line voiced dialog generation seed"),
    DigestSite("src/sprites/content.py", "_seed_for", 1, "keep",
               "output seed: per-entity sprite-bank base seed"),
    DigestSite("src/blender/seeding.py", "derive_seed", 1, "keep",
               "output seed: every procedural mesh's recipe/diffusion RNG fans "
               "out from it"),
    DigestSite("src/unity_terraingen.py", "project_seed_from_name", 1, "keep",
               "output seed: per-project terrain seed base"),
    DigestSite("src/unity_terraingen.py", "seed_for_region", 1, "keep",
               "output seed: per-region terrain seed"),
    DigestSite("src/unity_entity_asset_resolver.py", "_stable_seed", 1, "keep",
               "output seed: the catalog variety-rotation draw that picks each "
               "entity's model"),
    DigestSite("src/unity_greybox.py", "_seed_from_schema", 1, "keep",
               "output seed: project-name fallback for the deterministic "
               "dungeon layout when no explicit seed is supplied"),

    # ── wrapped / machine-stable local keys ───────────────────────────────────
    DigestSite("src/blender/cache.py", "BlenderCache.fingerprint", 1, "keep",
               "wrapped: Track A2 adopts it as an asset's recipe_hash; versioned "
               "by CACHE_VERSION"),
    DigestSite("src/blender/cache.py", "BlenderCache.geo_fingerprint", 1, "keep",
               "wrapped: the pre-rig mesh reuse key Track A2 adopts; versioned "
               "by GEO_FP_VERSION"),
    DigestSite("src/blender/cache.py", "_file_md5", 1, "keep",
               "wrapped: the Blender cache's content-addressed file store key"),
    DigestSite("src/blender/cache.py", "_source_hash", 1, "keep",
               "wrapped: folds the emitted-script template into every Blender "
               "cache fingerprint"),
    DigestSite("src/blender/cache.py", "_recipe_hash", 1, "keep",
               "wrapped: folds the procedural recipe source into every Blender "
               "cache fingerprint"),
    DigestSite("src/audio/backends.py", "CachingClient._key", 1, "keep",
               "local cache key over (adapter, kind, params) — content-derived "
               "and machine-stable; changing it only forces a regen"),
    DigestSite("src/audio/resolver.py", "_cache_key", 1, "keep",
               "local cache key over the concept text — content-derived and "
               "machine-stable"),
    DigestSite("src/unity_semantic_match.py", "catalog_content_hash", 1, "keep",
               "local cache key over catalog rows with catalog-RELATIVE paths — "
               "already machine-stable"),
    DigestSite("src/unity_entity_asset_resolver.py", "resolve_entity_assets._register",
               1, "keep",
               "emitted file-name tag over the catalog-RELATIVE path — already "
               "machine-stable; changing it only renames outputs"),

    # ── migrate ───────────────────────────────────────────────────────────────
    # None open. A4 retired the two absolute-path digests
    # (unity_sceneinput._cache_key, unity_entity_asset_resolver.
    # _register_authored) onto src.ident.fhash / chash.
)
