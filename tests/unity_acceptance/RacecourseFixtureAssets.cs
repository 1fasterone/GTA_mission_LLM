// Engineer-authored controlled test assets; not Pipeline LLM output.
using UnityEngine;
using UnityEditor;
using System.IO;

public static class RacecourseFixtureAssets
{
    static void Box(Transform parent, string name, Vector3 center, Vector3 size)
    {
        var go = GameObject.CreatePrimitive(PrimitiveType.Cube);
        go.name = name;
        go.transform.SetParent(parent);
        go.transform.localPosition = center;
        go.transform.localScale = size;
    }

    public static void Build()
    {
        Directory.CreateDirectory("Assets/Resources/Acceptance");
        var gate = new GameObject("Gate");
        Box(gate.transform, "Left", new Vector3(-6, 3, 0), new Vector3(.3f, 6, .3f));
        Box(gate.transform, "Right", new Vector3(6, 3, 0), new Vector3(.3f, 6, .3f));
        Box(gate.transform, "Top", new Vector3(0, 6, 0), new Vector3(12, .3f, .3f));
        PrefabUtility.SaveAsPrefabAsset(gate, "Assets/Resources/Acceptance/Gate.prefab");
        Object.DestroyImmediate(gate);
        var road = new GameObject("Road");
        Box(road.transform, "Deck", new Vector3(0, -.1f, 0), new Vector3(10, .2f, 2));
        PrefabUtility.SaveAsPrefabAsset(road, "Assets/Resources/Acceptance/Road.prefab");
        Object.DestroyImmediate(road);
        var prop = new GameObject("Prop");
        Box(prop.transform, "Post", new Vector3(0, .5f, 0), new Vector3(.2f, 1, .2f));
        PrefabUtility.SaveAsPrefabAsset(prop, "Assets/Resources/Acceptance/Prop.prefab");
        Object.DestroyImmediate(prop);
        AssetDatabase.SaveAssets();
        AssetDatabase.Refresh();
        Debug.Log("Acceptance fixture assets built");
    }
}
