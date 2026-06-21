// ProcGenImporter.cs — auto-configures procedurally generated Blender FBX assets.
//
// Dropped into Assets/Editor/ by the model micro's Unity overlay. On import of any
// FBX under Assets/BlenderAssets/ it:
//   * sets the rig to Humanoid when a "Hips" bone is present (so the proximity-
//     skinned character is a valid avatar and Mixamo-style clips retarget onto it),
//   * wires a convex MeshCollider from the COL_* hull mesh (and hides its renderer),
//   * builds a LODGroup from the LOD0_*/LOD1_* renderers emitted by Blender.
//
// Naming conventions match src/blender/base_models.py (make_lods / collider_hull).
using System.Collections.Generic;
using System.Linq;
using UnityEditor;
using UnityEngine;

public class ProcGenImporter : AssetPostprocessor
{
    static bool UnderProcGen(string path)
    {
        return path.Replace('\\', '/').Contains("/BlenderAssets/");
    }

    void OnPreprocessModel()
    {
        if (!UnderProcGen(assetPath)) return;
        var mi = (ModelImporter)assetImporter;
        mi.globalScale = 1.0f;
        mi.importNormals = ModelImporterNormals.Calculate;
    }

    void OnPostprocessModel(GameObject root)
    {
        if (!UnderProcGen(assetPath)) return;

        // Humanoid rig if the skeleton has a Hips bone.
        var hips = root.GetComponentsInChildren<Transform>(true)
                       .FirstOrDefault(t => t.name == "Hips");
        if (hips != null)
        {
            var mi = (ModelImporter)assetImporter;
            if (mi.animationType != ModelImporterAnimationType.Human)
                mi.animationType = ModelImporterAnimationType.Human; // triggers avatar build + reimport
        }

        WireColliders(root);
        WireLODs(root);
    }

    static void WireColliders(GameObject root)
    {
        foreach (var mf in root.GetComponentsInChildren<MeshFilter>(true))
        {
            if (!mf.name.StartsWith("COL_")) continue;
            var rend = mf.GetComponent<MeshRenderer>();
            if (rend != null) rend.enabled = false;
            var mc = mf.GetComponent<MeshCollider>() ?? mf.gameObject.AddComponent<MeshCollider>();
            mc.sharedMesh = mf.sharedMesh;
            mc.convex = true;
        }
    }

    static void WireLODs(GameObject root)
    {
        var lod0 = new List<Renderer>();
        var lod1 = new List<Renderer>();
        foreach (var r in root.GetComponentsInChildren<Renderer>(true))
        {
            if (r.name.StartsWith("LOD0_")) lod0.Add(r);
            else if (r.name.StartsWith("LOD1_")) lod1.Add(r);
        }
        if (lod0.Count == 0) return;

        var group = root.GetComponent<LODGroup>() ?? root.AddComponent<LODGroup>();
        var lods = lod1.Count > 0
            ? new[] { new LOD(0.45f, lod0.ToArray()), new LOD(0.08f, lod1.ToArray()) }
            : new[] { new LOD(0.08f, lod0.ToArray()) };
        group.SetLODs(lods);
        group.RecalculateBounds();
    }
}
