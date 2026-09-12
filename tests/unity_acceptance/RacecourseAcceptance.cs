// Real Unity PlayMode checks of compiler-generated scripts and scene.
using System;
using System.Collections;
using System.IO;
using System.Reflection;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;

public class RacecourseAcceptance
{
    [Serializable] public class Group { public string name; public Vector3[] positions; }
    [Serializable] public class Expected { public Group[] groups; }
    static Type Runtime(string name) => Type.GetType(name + ", Assembly-CSharp", true);
    static Component Find(string name) => UnityEngine.Object.FindFirstObjectByType(Runtime(name)) as Component;

    [UnityTest]
    public IEnumerator GeneratedCourseAndBridgesWork()
    {
        yield return SceneManager.LoadSceneAsync("Race");
        yield return null;
        yield return null;
        var expected = JsonUtility.FromJson<Expected>(File.ReadAllText("expected.json"));
        foreach (var group in expected.groups)
        {
            var parent = GameObject.Find(group.name);
            Assert.IsNotNull(parent, group.name);
            Assert.AreEqual(group.positions.Length, parent.transform.childCount, group.name);
            for (int i = 0; i < group.positions.Length; i++)
            {
                var go = parent.transform.GetChild(i);
                Assert.Less(Vector2.Distance(new Vector2(go.position.x, go.position.z),
                    new Vector2(group.positions[i].x, group.positions[i].z)), .05f, go.name + " XZ");
                // Prefab pivots start at the bottom (gate top has .15m overhang).
                Assert.Less(Mathf.Abs(go.position.y - group.positions[i].y), .2f, go.name + " height");
                if (group.name == "Barriers" && i > 0)
                    Assert.LessOrEqual(Vector3.Distance(go.position, parent.transform.GetChild(i - 1).position),
                                      2.01f, "Curved/elevated boundary gap");
            }
        }
        Physics.SyncTransforms();
        foreach (string name in new[] { "CheckpointBanners", "FinishArch" })
        {
            var gates = GameObject.Find(name).transform;
            foreach (Transform gate in gates)
            {
                // A car-sized volume can cross the opening without contacting the gate.
                for (float z = -3; z <= 3; z += .5f)
                {
                    var center = gate.position + Vector3.up + gate.forward * z;
                    foreach (var hit in Physics.OverlapBox(center, new Vector3(.85f, .6f, 2f), gate.rotation))
                        Assert.IsFalse(hit.transform.IsChildOf(gate), gate.name + " blocks the vehicle");
                }
            }
        }
        var inv = Find("TestInventory");
        var health = Find("TestHealth");
        var objectives = Find("TestObjectives");
        var door = Find("TestDoor");
        Assert.IsNotNull(inv); Assert.IsNotNull(health); Assert.IsNotNull(objectives); Assert.IsNotNull(door);
        health.GetType().GetMethod("TakeDamage").Invoke(health, new object[] { 30f });
        var before = (float)health.GetType().GetProperty("CurrentHealth").GetValue(health);
        var itemType = Runtime("TestInventoryItemDefinition");
        foreach (string effect in new[] { "Heal", "QuestFlag", "Unlock" })
        {
            var item = ScriptableObject.CreateInstance(itemType);
            itemType.GetField("effect").SetValue(item, Enum.Parse(itemType.GetNestedType("Effect"), effect));
            itemType.GetField("amount").SetValue(item, 10f);
            itemType.GetField("targetId").SetValue(item, "key");
            Assert.IsTrue((bool)inv.GetType().GetMethod("Add").Invoke(inv, new object[] { item, 1 }));
            Assert.IsTrue((bool)inv.GetType().GetMethod("UseItem").Invoke(inv, new object[] { item }));
            UnityEngine.Object.Destroy(item);
        }
        Assert.AreEqual(before + 10, health.GetType().GetProperty("CurrentHealth").GetValue(health));
        Assert.IsTrue((bool)objectives.GetType().GetMethod("HasFlag").Invoke(objectives, new object[] { "key" }));
        Assert.IsFalse((bool)door.GetType().GetField("locked", BindingFlags.Instance | BindingFlags.NonPublic).GetValue(door));
        // Record the real rendered fixture, not a diagram of expected positions.
        var camera = Camera.main;
        Assert.IsNotNull(camera);
        camera.transform.position = new Vector3(60, 105, -45);
        camera.transform.LookAt(new Vector3(35, 8, 40));
        camera.orthographic = true;
        camera.orthographicSize = 75;
        var target = new RenderTexture(1280, 960, 24);
        camera.targetTexture = target;
        camera.Render();
        var previous = RenderTexture.active;
        RenderTexture.active = target;
        var texture = new Texture2D(1280, 960, TextureFormat.RGB24, false);
        texture.ReadPixels(new Rect(0, 0, 1280, 960), 0, 0);
        texture.Apply();
        File.WriteAllBytes("acceptance.png", texture.EncodeToPNG());
        RenderTexture.active = previous;
        camera.targetTexture = null;
        UnityEngine.Object.Destroy(texture);
        UnityEngine.Object.Destroy(target);
        LogAssert.NoUnexpectedReceived();
    }
}
