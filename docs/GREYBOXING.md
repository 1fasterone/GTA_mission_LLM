# Greyboxing: Greybox+ Philosophy

## 1. Greybox+ is not art

Greybox+ is still a rough, playable 3D layout built from untextured primitive
geometry. It does not add final materials, textures, lighting, shaders, art
assets, gameplay scripts, enemies, items, or props.

The difference is intentional shape language. Primitives establish:

- recognizable silhouettes;
- industrial massing;
- believable scale;
- early navigation cues; and
- spatial identity.

Think in big ducts, chunky supports, blocky machinery, oversized vents, simple
tanks, catwalks, railings, and ramps. They remain primitives, but the shapes do
more work.

## 2. Primitive asset library

Use cubes, cylinders, and planes purposefully.

| Category | Primitive use |
| --- | --- |
| Structural | Mega-cubes for buildings, hangars, and walls; tall thin cubes for pillars and supports; long cubes for ducts, pipes, and beams; wide planes for floors, catwalks, and ramps; cylinders for tanks, silos, and industrial drums. |
| Silhouette | Stacked cubes for machinery blocks; offset cylinders for turbines; angled planes for roof shapes and ramps; large hollow cubes for tunnel entrances. |
| Navigation | Low cubes for cover; tall cubes for sightline blockers; thin planes for railings and barriers; wide cylinders for round arenas. |

Everything remains untextured, unlit, and unshaded. The intent comes from
shape, scale, spacing, and repetition rather than art treatment.

## 3. Greybox+ hierarchy

Greybox+ uses a deterministic hierarchy that can be realized by Unity or any
other backend:

```text
GreyboxPlus_Level/
  Geometry/
    Macro/
      Buildings/
      TerrainBlocks/
      Elevation/
    Meso/
      Pipes/
      Tanks/
      Machinery/
      Catwalks/
    Micro/
      Cover/
      Blockers/
      Railings/
  Markers/
    Spawns/
    AI_Nodes/
    CameraVolumes/
```

The existing dungeon-specific root remains
`Dungeon_Greybox/{Geometry,Rooms,Corridors,Elevation,Markers}` until its IR is
explicitly upgraded to this Greybox+ hierarchy. A document does not change the
current serialized schema or generator contract.

## 4. Macro layout pass: big shapes first

Build the city blocks of the level first:

- giant cubes for buildings;
- large cylinders for tanks;
- long cubes for ducts; and
- elevated planes for platforms.

The goal is to establish silhouette, massing, and the large-scale route before
adding detail.

## 5. Meso layout pass: industrial identity

Add primitive elements that make the spaces recognizable:

- overhead pipes made from long cubes;
- support pillars made from thin tall cubes;
- catwalks made from thin planes;
- ramps made from angled planes;
- machinery blocks made from stacked cubes; and
- vents made from small cylinders.

They are still primitives, but they give the layout an industrial identity.

## 6. Micro gameplay pass

Add small primitive geometry that affects movement and combat readability:

- cover blocks;
- sightline blockers;
- jumpable platforms;
- crouch tunnels;
- choke points; and
- traversal ramps.

The result remains grey, but becomes playable and readable from player height.

## 7. Deterministic validation

Run the normal deterministic checks:

- grid alignment;
- corridor width;
- elevation reachability;
- loop connectivity;
- no accidental overlaps; and
- silhouette readability from player height.

Topology remains compiler truth. The visual hierarchy and primitive transforms
must be serialized so the same input produces the same blockout.

## 8. Playtest spatial character

This is not an art test. Test:

- readability;
- navigation clarity;
- landmark recognition;
- industrial identity;
- pacing and flow; and
- vertical traversal.

If a player can say “the tank area,” “the pipe corridor,” or “the catwalk
zone,” the blockout has succeeded.

## 9. Iterate fast

Greybox+ remains disposable. Resize cubes, move cylinders, delete ducts, shift
platforms, and adjust silhouettes without art constraints.

## 10. Transition to final art

When the spatial layout feels right:

1. Freeze the accepted transforms.
2. Export the geometry.
3. Hand it off to modular-kit creation.
4. Preserve the validated silhouette and massing in final art.
