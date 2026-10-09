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

    private static string ReadString(MonoBehaviour rig, string propertyName)
    {
        var property = rig.GetType().GetProperty(
            propertyName, BindingFlags.Instance | BindingFlags.Public);
        Assert.IsNotNull(property,
            "Camera Contract v1 is missing public " + propertyName + ".");
        Assert.AreEqual(typeof(string), property.PropertyType,
            propertyName + " must be a string.");
        return (string)property.GetValue(rig);
    }

    private static Transform ReadResolvedTarget(MonoBehaviour rig)
    {
        var property = rig.GetType().GetProperty(
            "CameraResolvedTarget", BindingFlags.Instance | BindingFlags.Public);
        Assert.IsNotNull(property,
            "Camera Contract v1 must expose its resolved target for acceptance.");
        Assert.AreEqual(typeof(Transform), property.PropertyType,
            "CameraResolvedTarget must be a Transform.");
        return (Transform)property.GetValue(rig);
    }

    private static void AssertTargetIsFramed(Camera camera, Transform target, string mode,
                                             bool requiresLookAt = true)
    {
        Vector3 toTarget = target.position - camera.transform.position;
        Assert.Greater(toTarget.sqrMagnitude, 0.0001f,
            mode + " target overlaps the camera, so framing cannot be verified.");
        if (requiresLookAt)
        {
            float angle = Vector3.Angle(camera.transform.forward, toTarget);
            Assert.LessOrEqual(angle, 2.0f,
                mode + " camera is not aimed at its resolved target.");
        }

        Vector3 viewport = camera.WorldToViewportPoint(target.position);
        Assert.Greater(viewport.z, 0f, mode + " target is behind the camera.");
        Assert.That(viewport.x, Is.InRange(0.05f, 0.95f),
            mode + " target is outside the horizontal frame.");
        Assert.That(viewport.y, Is.InRange(0.05f, 0.95f),
            mode + " target is outside the vertical frame.");
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
        yield return null;
        yield return null;
        var camera = Camera.main;
        Assert.IsNotNull(camera, "Generated scene has no Main Camera.");
        var rig = FindContractCamera(camera);
        Assert.IsNotNull(rig, "No generated CameraRig contract was found.");

        var mode = ReadMode(rig);
        Assert.IsNotEmpty(mode, "CameraModeID must identify the active camera mode.");
        var player = GameObject.Find("Player");
        if (mode == "BoardFraming")
            Assert.Ignore("Board framing has no avatar anchor by design.");
        Assert.IsNotNull(player, mode + " requires a generated Player anchor.");

        string targetType = ReadString(rig, "CameraPrimaryTargetType");
        string targetRef = ReadString(rig, "CameraPrimaryTargetRef");
        Transform resolvedTarget = ReadResolvedTarget(rig);
        Assert.IsNotNull(resolvedTarget,
            mode + " did not resolve its declared primary target.");

        if (mode == "FirstPerson")
        {
            var eye = player.transform.Find("Eye");
            Assert.IsNotNull(eye, "FirstPerson contract requires Player/Eye.");
            Assert.AreEqual("anchor", targetType,
                "FirstPerson must target its Player/Eye anchor.");
            Assert.AreEqual("Player/Eye", targetRef,
                "FirstPerson must name Player/Eye as its primary target.");
            Assert.AreSame(eye, resolvedTarget,
                "FirstPerson resolved a target other than Player/Eye.");
            Assert.Less(
                Vector3.Distance(camera.transform.position, eye.position),
                0.1f,
                "FirstPerson camera is not mounted on Player/Eye.");
            Assert.Less(Quaternion.Angle(camera.transform.rotation, eye.rotation), 0.5f,
                "FirstPerson camera does not copy the Player/Eye pose.");
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
            Assert.AreEqual("anchor", targetType,
                "Follow2D must frame its Player anchor directly.");
            Assert.AreEqual("Player", targetRef,
                "Follow2D must name Player as its primary target.");
            Assert.AreSame(player.transform, resolvedTarget,
                "Follow2D resolved a target other than Player.");
            Assert.IsTrue(camera.orthographic, "Follow2D must use orthographic projection.");
            Assert.Less(
                Quaternion.Angle(camera.transform.rotation, Quaternion.identity),
                0.5f,
                "Follow2D must keep a fixed identity orientation.");
            AssertTargetIsFramed(camera, resolvedTarget, mode, requiresLookAt: false);
        }

        if (mode == "TopDown" || mode == "Isometric" || mode == "ThirdPersonChase")
        {
            Assert.AreEqual("camera_focus", targetType,
                mode + " must use CameraFocus rather than the placement offset as its target.");
            Assert.AreEqual("CameraFocus", targetRef,
                mode + " must name the stable CameraFocus target.");
            var focus = player.transform.Find("CameraFocus");
            Assert.IsNotNull(focus, mode + " did not create CameraFocus under Player.");
            Assert.AreSame(focus, resolvedTarget,
                mode + " resolved a target other than Player/CameraFocus.");
            AssertTargetIsFramed(camera, resolvedTarget, mode);
        }
    }
}
