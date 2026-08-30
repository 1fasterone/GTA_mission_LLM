"""
Deterministic Unity Terrain generator — heightmap + splatmap, no LLM.

Unlike every other micro-generator in this repo, this one is world/region
scale, not per-entity, and its output artifact isn't C# — it's numeric
heightmap/splatmap data plus a fixed Editor-time importer script. Unity's
`Terrain` component is backed by binary `TerrainData` (heights, alphamaps)
that can't be hand-authored as text the way a `.cs.j2` MonoBehaviour can, so
the deterministic-Python-only rule (CLAUDE.md: "Steps 4-7 remain
deterministic") is honored here differently than elsewhere: Python bakes the
DATA (16-bit heightmap PNG + packed-RGBA splatmap PNG), and a fixed,
non-LLM-authored `TerrainBuilder.cs` (templates/unity/micro/terrain/) reads
that data at Unity import time and calls `TerrainData.SetHeights`/
`SetAlphamaps`.

Landing spot: `WorldSpec.regions` (src/unity_schema.py) already carries
`bounds: [xmin, zmin, xmax, zmax]` and `biome: str` per region — exactly the
input this needs, with no schema changes. See `terrain_jobs_for_world`.

v1 scope, documented rather than silently assumed:
  - One independently-seeded heightmap per region. No cross-region seam
    blending — two adjacent regions will NOT tile continuously. A future
    version could derive octave phase from world-space bounds instead of a
    per-region seed if that's ever needed.
  - Heights come from a single fractal value-noise field (numpy fallback —
    no `noise`/`perlin-noise` dependency added). Splatmap layer weights are a
    pure function of the baked heightmap (height-band blend), not a second
    independent noise field, so texture bands always agree with the terrain
    they're painted on.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

# Unity Terrain heightmap resolution must be 2^n + 1.
VALID_RESOLUTIONS: tuple[int, ...] = (33, 65, 129, 257, 513, 1025, 2049, 4097)
DEFAULT_RESOLUTION = 129
DEFAULT_HEIGHT_SCALE = 30.0

# Biomes with no plausible ground plane — a region opts OUT of terrain
# generation by declaring one of these (CLAUDE.md: abstain loudly rather
# than baking a floor into an interior/space scene that doesn't want one).
NO_GROUND_BIOMES: frozenset[str] = frozenset({
    "interior", "space", "void", "underwater_interior", "station",
})

# Biome -> ground texture layers, low elevation first. Unbounded custom biome
# strings (free LLM text) fall back to _DEFAULT_LAYERS rather than guessing.
BIOME_LAYERS: dict[str, list[str]] = {
    "urban":      ["concrete", "grass"],
    "forest":     ["grass", "dirt", "rock"],
    "desert":     ["sand", "rock"],
    "mountain":   ["rock", "snow", "grass"],
    "swamp":      ["mud", "grass"],
    "wetland":    ["mud", "grass"],
    "tundra":     ["snow", "rock"],
    "grassland":  ["grass", "dirt"],
    "beach":      ["sand", "grass"],
    "coastal":    ["sand", "grass"],
    "volcanic":   ["rock", "ash"],
    "arctic":     ["snow", "rock"],
}
_DEFAULT_LAYERS: list[str] = ["grass", "dirt", "rock"]

# Splatmap layers pack one per RGBA channel of a single PNG (Unity's own
# convention for a single-pass, <=4-layer terrain alphamap).
_MAX_SPLATMAP_LAYERS = 4


def layers_for_biome(biome: str) -> list[str]:
    return BIOME_LAYERS.get((biome or "").strip().lower(), _DEFAULT_LAYERS)


def project_seed_from_name(project_name: str) -> int:
    """Stable per-project seed base, so re-running the same project name
    reproduces the same terrain (same determinism contract as everything
    else this generator does) while two DIFFERENT projects that happen to
    both name a region e.g. "Region" don't collide onto identical terrain."""
    digest = hashlib.sha256(project_name.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def seed_for_region(name: str, project_seed: int = 0) -> int:
    """Stable seed derived from a region name — `hash()` is process-salted in
    Python and would break the "same input -> same output" contract every
    other deterministic generator in this repo relies on."""
    digest = hashlib.sha256(f"{project_seed}:{name}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


@dataclass(frozen=True)
class TerrainSpec:
    """One region's terrain generation job — a pure function's worth of input."""
    name:         str
    bounds:       tuple[float, float, float, float]   # xmin, zmin, xmax, zmax
    biome:        str
    seed:         int
    resolution:   int   = DEFAULT_RESOLUTION
    height_scale: float = DEFAULT_HEIGHT_SCALE

    @property
    def width(self) -> float:
        return self.bounds[2] - self.bounds[0]

    @property
    def length(self) -> float:
        return self.bounds[3] - self.bounds[1]

    @property
    def layers(self) -> list[str]:
        return layers_for_biome(self.biome)


def terrain_jobs_for_world(world: Any, *, project_seed: int = 0) -> list[TerrainSpec]:
    """One TerrainSpec per WorldRegion that plausibly has ground underfoot.

    Deterministic, no LLM. A region is skipped — never silently defaulted —
    for exactly two named reasons: a no-ground biome (NO_GROUND_BIOMES) or a
    degenerate (zero/negative-area) bounds rect. `world` is `schema.world`
    (WorldSpec | None); an absent world or empty region list returns [].
    """
    regions = list(getattr(world, "regions", None) or [])
    if not regions:
        return []
    world_biome = str(getattr(world, "biome", "") or "")
    jobs: list[TerrainSpec] = []
    for region in regions:
        biome = str(getattr(region, "biome", "") or world_biome).strip().lower()
        if biome in NO_GROUND_BIOMES:
            continue
        bounds = tuple(float(v) for v in getattr(region, "bounds", (0.0, 0.0, 0.0, 0.0)))
        xmin, zmin, xmax, zmax = bounds
        if xmax <= xmin or zmax <= zmin:
            continue
        name = str(getattr(region, "name", "") or "Region")
        jobs.append(TerrainSpec(
            name=name,
            bounds=(xmin, zmin, xmax, zmax),
            biome=biome or "grassland",
            seed=seed_for_region(name, project_seed),
        ))
    return jobs


# ── Heightmap ──────────────────────────────────────────────────────────────

def _octave_grid(rng: np.random.Generator, size: int, resolution: int) -> np.ndarray:
    """A `size` x `size` grid of random values in [0,1], bilinearly upsampled
    to (resolution, resolution) — a standard value-noise octave with no
    external noise-library dependency."""
    coarse = rng.random((size, size)).astype("float32")
    if size == resolution:
        return coarse
    img = Image.fromarray(coarse, mode="F").resize((resolution, resolution),
                                                    resample=Image.Resampling.BILINEAR)
    return np.asarray(img, dtype="float32")


def generate_heightmap(seed: int, resolution: int = DEFAULT_RESOLUTION, *,
                       octaves: int = 4, persistence: float = 0.5,
                       lacunarity: float = 2.0) -> np.ndarray:
    """Deterministic fractal value-noise heightmap, normalized to [0, 1].

    Same (seed, resolution, octaves, persistence, lacunarity) -> byte-identical
    array, every call, every machine — `np.random.default_rng(seed)` is a
    fixed, versioned PRNG (unlike the legacy global `np.random` state).
    """
    if resolution not in VALID_RESOLUTIONS:
        raise ValueError(
            f"resolution {resolution} is not a valid Unity heightmap size "
            f"(2^n + 1): choose one of {VALID_RESOLUTIONS}"
        )
    rng = np.random.default_rng(seed)
    heightmap = np.zeros((resolution, resolution), dtype="float64")
    amplitude, total_amplitude, grid_size = 1.0, 0.0, 3
    for _ in range(max(1, octaves)):
        heightmap += amplitude * _octave_grid(rng, min(grid_size, resolution), resolution)
        total_amplitude += amplitude
        amplitude *= persistence
        grid_size = max(3, int(round(grid_size * lacunarity)))
    heightmap /= total_amplitude

    hmin, hmax = float(heightmap.min()), float(heightmap.max())
    if hmax > hmin:
        heightmap = (heightmap - hmin) / (hmax - hmin)
    else:
        heightmap[:] = 0.5   # a degenerate (single-octave, flat) field
    return heightmap


# ── Splatmap ───────────────────────────────────────────────────────────────

def generate_splatmap(heightmap: np.ndarray, biome: str) -> tuple[np.ndarray, list[str]]:
    """Height-band texture-layer weights for `biome`, shape (res, res, n).

    A pure function of the already-baked heightmap (not a second independent
    noise field) so the texture bands always agree with the terrain they're
    painted on. Weights sum to 1.0 at every texel by construction (Gaussian
    falloff from each layer's height-band center, normalized) — never a
    zero-sum edge case to special-case.
    """
    layers = layers_for_biome(biome)
    n = len(layers)
    if n > _MAX_SPLATMAP_LAYERS:
        raise ValueError(
            f"biome {biome!r} declares {n} layers; single-pass PNG packing "
            f"supports at most {_MAX_SPLATMAP_LAYERS}"
        )
    if n == 1:
        return np.ones(heightmap.shape + (1,), dtype="float64"), layers

    centers = np.linspace(0.0, 1.0, n)
    sigma = 1.0 / (2 * (n - 1))
    raw = np.stack(
        [np.exp(-((heightmap - c) ** 2) / (2 * sigma ** 2)) for c in centers],
        axis=-1,
    )
    raw = np.clip(raw, 1e-6, None)   # never exactly zero -> sum-normalize is always safe
    weights = raw / raw.sum(axis=-1, keepdims=True)
    return weights, layers


# ── PNG encoding ───────────────────────────────────────────────────────────

def write_heightmap_png(heightmap: np.ndarray, path: Path) -> None:
    """Height packed as two 8-bit channels (R=high byte, G=low byte) in a
    standard RGBA32 PNG — NOT a 16-bit grayscale PNG. Unity's TextureImporter
    16-bit-grayscale import path varies by version/platform in ways this repo
    has no live Editor to verify against; reading two raw 0-255 byte channels
    back with `sRGBTexture: 0` (linear, no gamma decode — see the .meta this
    generator writes) is exact regardless of Editor version. Reconstructed in
    C# as `(r*255 * 256 + g*255) / 65535f` — see TerrainBuilder.cs.j2."""
    res = heightmap.shape[0]
    packed = np.clip(np.round(heightmap * 65535.0), 0, 65535).astype("uint16")
    channels = np.zeros((res, res, 4), dtype="uint8")
    channels[:, :, 0] = (packed >> 8).astype("uint8")    # high byte
    channels[:, :, 1] = (packed & 0xFF).astype("uint8")  # low byte
    channels[:, :, 3] = 255
    Image.fromarray(channels, mode="RGBA").save(path)


def write_splatmap_png(splatmap: np.ndarray, path: Path) -> None:
    """Pack up to 4 layers one per RGBA channel. A biome with fewer than 4
    layers leaves the remaining channels at 0 — TerrainBuilder.cs.j2 only
    ever reads the first `len(layers)` channels back, so an unused zeroed
    channel is inert data, never mistaken for a real (zero-weight) layer."""
    res = splatmap.shape[0]
    n = splatmap.shape[2]
    if n > _MAX_SPLATMAP_LAYERS:
        raise ValueError(f"splatmap has {n} layers; PNG packing supports at most {_MAX_SPLATMAP_LAYERS}")
    channels = np.zeros((res, res, 4), dtype="uint8")
    for i in range(n):
        channels[:, :, i] = np.clip(np.round(splatmap[:, :, i] * 255.0), 0, 255).astype("uint8")
    Image.fromarray(channels, mode="RGBA").save(path)


def readable_texture_meta(guid: str) -> str:
    """TextureImporter .meta forcing Read/Write enabled + zero compression +
    linear (no sRGB gamma decode) — required for both heightmap.png and
    splatmap.png, whose raw RGBA bytes ARE the data (see write_heightmap_png/
    write_splatmap_png). Every OTHER texture this project copies lets Unity
    generate its .meta on first import (unity_generator.py's comment: "Unity
    generates the TextureImporter .metas itself on first open") — that
    default (compressed, not readable, sRGB) would silently corrupt these
    two, so they get an explicit .meta instead, the same way every other
    deterministic asset in this project does.
    """
    return (
        "fileFormatVersion: 2\n"
        f"guid: {guid}\n"
        "TextureImporter:\n"
        "  internalIDToNameTable: []\n"
        "  externalObjects: {}\n"
        "  serializedVersion: 12\n"
        "  mipmaps:\n"
        "    mipMapMode: 0\n"
        "    enableMipMap: 0\n"
        "    sRGBTexture: 0\n"
        "    linearTexture: 1\n"
        "    fadeOut: 0\n"
        "    borderMipMap: 0\n"
        "    mipMapsPreserveCoverage: 0\n"
        "    alphaTestReferenceValue: 0.5\n"
        "    mipMapFadeDistanceStart: 1\n"
        "    mipMapFadeDistanceEnd: 3\n"
        "  bumpmap:\n"
        "    convertToNormalMap: 0\n"
        "    externalNormalMap: 0\n"
        "    heightScale: 0.25\n"
        "    normalMapFilter: 0\n"
        "    flipGreenChannel: 0\n"
        "  isReadable: 1\n"
        "  streamingMipmaps: 0\n"
        "  streamingMipmapsPriority: 0\n"
        "  vTOnly: 0\n"
        "  ignoreMipmapLimit: 0\n"
        "  grayScaleToAlpha: 0\n"
        "  generateCubemap: 6\n"
        "  cubemapConvolution: 0\n"
        "  seamlessCubemap: 0\n"
        "  textureFormat: 1\n"
        "  maxTextureSize: 4096\n"
        "  textureSettings:\n"
        "    serializedVersion: 2\n"
        "    filterMode: 0\n"
        "    aniso: 0\n"
        "    mipBias: 0\n"
        "    wrapU: 1\n"
        "    wrapV: 1\n"
        "    wrapW: 1\n"
        "  nPOTScale: 0\n"
        "  lightmap: 0\n"
        "  compressionQuality: 100\n"
        "  spriteMode: 0\n"
        "  spriteExtrude: 1\n"
        "  spriteMeshType: 1\n"
        "  alignment: 0\n"
        "  spritePivot: {x: 0.5, y: 0.5}\n"
        "  spritePixelsToUnits: 100\n"
        "  spriteBorder: {x: 0, y: 0, z: 0, w: 0}\n"
        "  spriteGenerateFallbackPhysicsShape: 1\n"
        "  alphaUsage: 1\n"
        "  alphaIsTransparency: 0\n"
        "  spriteTessellationDetail: -1\n"
        "  textureType: 0\n"
        "  textureShape: 1\n"
        "  singleChannelComponent: 0\n"
        "  flipbookRows: 1\n"
        "  flipbookColumns: 1\n"
        "  maxTextureSizeSet: 0\n"
        "  compressionQualitySet: 0\n"
        "  textureFormatSet: 0\n"
        "  ignorePngGamma: 0\n"
        "  applyGammaDecoding: 0\n"
        "  platformSettings:\n"
        "  - serializedVersion: 3\n"
        "    buildTarget: DefaultTexturePlatform\n"
        "    maxTextureSize: 4096\n"
        "    resizeAlgorithm: 0\n"
        "    textureFormat: -1\n"
        "    textureCompression: 0\n"
        "    compressionQuality: 50\n"
        "    crunchedCompression: 0\n"
        "    allowsAlphaSplitting: 0\n"
        "    overridden: 0\n"
        "    androidETC2FallbackOverride: 0\n"
        "    forceMaximumCompressionQuality_BC6H_BC7: 0\n"
        "  spriteSheet:\n"
        "    serializedVersion: 2\n"
        "    sprites: []\n"
        "    outline: []\n"
        "    physicsShape: []\n"
        "    bones: []\n"
        "    spriteID: \n"
        "    internalID: 0\n"
        "    vertices: []\n"
        "    indices: \n"
        "    edges: []\n"
        "    weights: []\n"
        "    secondaryTextures: []\n"
        "  spritePackingTag: \n"
        "  pSDRemoveMatte: 0\n"
        "  pSDShowRemoveMatteOption: 0\n"
        "  userData: \n"
        "  assetBundleName: \n"
        "  assetBundleVariant: \n"
    )


@dataclass
class TerrainBake:
    """A fully computed terrain job, ready to write to disk."""
    spec:      TerrainSpec
    heightmap: np.ndarray
    splatmap:  np.ndarray
    layers:    list[str] = field(default_factory=list)


def bake_terrain(spec: TerrainSpec) -> TerrainBake:
    heightmap = generate_heightmap(spec.seed, spec.resolution)
    splatmap, layers = generate_splatmap(heightmap, spec.biome)
    return TerrainBake(spec=spec, heightmap=heightmap, splatmap=splatmap, layers=layers)
