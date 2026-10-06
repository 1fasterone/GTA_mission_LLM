// PlayMode acceptance for composed projects using Camera Contract v1.
// The generated camera class name varies by project, so the probe discovers
// the stable public contract surface instead of guessing a class name.
using System.Collections;
using System.Reflection;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.TestTools;

public class CameraContractAcceptance
{
    private static MonoBehaviour FindContractCamera(Camera camera)
    {
        foreach (var component in camera.GetComponents<MonoBehaviour>())
        {
            if (component == null) continue;
            var property = component.GetType().GetProperty(
                "CameraModeID", BindingFlags.Instance | BindingFlags.Public);
            if (property != null && property.PropertyType == typeof(string))
                return component;
        }
        return null;
    }

    private static int CountContractCameras(Camera camera)
    {
        var count = 0;
        foreach (var component in camera.GetComponents<MonoBehaviour>())
        {
            if (component == null) continue;
            if (component.GetType().GetProperty(
                    "CameraModeID", BindingFlags.Instance | BindingFlags.Public) != null)
                count++;
        }
        return count;
    }

    private static string ReadMode(MonoBehaviour rig)
    {
        return (string)rig.GetType().GetProperty("CameraModeID").GetValue(rig);
    }

    [UnityTest]
    public IEnumerator ComposedSceneHasExactlyOneCameraContractOwner()
    {
        yield return null;
        var camera = Camera.main;
        Assert.IsNotNull(camera, "Generated scene has no Main Camera.");

        var contractCamera = FindContractCamera(camera);
        Assert.AreEqual(1, CountContractCameras(camera),
            "Generated scene must have exactly one Camera Contract owner.");
        Assert.IsNotNull(
            contractCamera,
            "Composed camera is missing the Camera Contract v1 public surface.");
        Assert.IsNotEmpty(ReadMode(contractCamera));
    }

    [UnityTest]
    public IEnumerator CameraContractMatchesTheGeneratedMode()
    {
        yield return null;
        var camera = Camera.main;
        Assert.IsNotNull(camera, "Generated scene has no Main Camera.");
        var rig = FindContractCamera(camera);
        Assert.IsNotNull(rig, "No generated CameraRig contract was found.");

        var mode = ReadMode(rig);
        var player = GameObject.Find("Player");
        if (mode == "BoardFraming")
            Assert.Ignore("Board framing has no avatar anchor by design.");
        Assert.IsNotNull(player, mode + " requires a generated Player anchor.");

        if (mode == "FirstPerson")
        {
            var eye = player.transform.Find("Eye");
            Assert.IsNotNull(eye, "FirstPerson contract requires Player/Eye.");
            Assert.Less(
                Vector3.Distance(camera.transform.position, eye.position),
                0.1f,
                "FirstPerson camera is not mounted on Player/Eye.");
        }
        else
        {
            var before = camera.transform.position;
            player.transform.position += Vector3.right;
            yield return null;
            yield return null;
            Assert.Greater(
                Vector3.Distance(before, camera.transform.position),
                0.001f,
                mode + " camera did not follow the Player anchor.");
        }

        if (mode == "Follow2D")
        {
            Assert.IsTrue(camera.orthographic, "Follow2D must use orthographic projection.");
            Assert.Less(
                Quaternion.Angle(camera.transform.rotation, Quaternion.identity),
                0.5f,
                "Follow2D must keep a fixed identity orientation.");
        }

        if (mode == "TopDown" || mode == "Isometric" || mode == "ThirdPersonChase")
        {
            var toPlayer = (player.transform.position - camera.transform.position).normalized;
            Assert.Greater(
                Vector3.Dot(camera.transform.forward, toPlayer),
                0.98f,
                mode + " camera is not facing its primary target.");
        }
    }
}
