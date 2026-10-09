// PlayMode matrix for the five controlled Camera Contract v1 fixture scripts.
// The script names and expected values below are test data, not pipeline output.
using System;
using System.Collections;
using System.Reflection;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.TestTools;

public class CameraContractFixtureMatrix
{
    private struct Fixture
    {
        public readonly string ClassName;
        public readonly string Mode;
        public readonly string TargetType;
        public readonly string TargetRef;
        public readonly bool IsFirstPerson;
        public readonly bool IsTwoD;

        public Fixture(string className, string mode, string targetType,
                       string targetRef, bool isFirstPerson = false,
                       bool isTwoD = false)
        {
            ClassName = className;
            Mode = mode;
            TargetType = targetType;
            TargetRef = targetRef;
            IsFirstPerson = isFirstPerson;
            IsTwoD = isTwoD;
        }
    }

    private static readonly Fixture[] Fixtures =
    {
        new Fixture("FollowChaseCamera", "ThirdPersonChase", "camera_focus", "CameraFocus"),
        new Fixture("FirstPersonEyeAnchor", "FirstPerson", "anchor", "Player/Eye",
                    isFirstPerson: true),
        new Fixture("Orthographic2DFollow", "Follow2D", "anchor", "Player", isTwoD: true),
        new Fixture("TopDownCamera", "TopDown", "camera_focus", "CameraFocus"),
        new Fixture("TopDownIsoPlayer", "Isometric", "camera_focus", "CameraFocus"),
    };

    private static Type FindFixtureType(string className)
    {
        foreach (var assembly in AppDomain.CurrentDomain.GetAssemblies())
        {
            var type = assembly.GetType(className);
            if (type != null && typeof(MonoBehaviour).IsAssignableFrom(type))
                return type;
        }
        Assert.Fail("Could not find generated fixture component " + className + ".");
        return null;
    }

    private static string ReadString(MonoBehaviour rig, string propertyName)
    {
        var property = rig.GetType().GetProperty(
            propertyName, BindingFlags.Instance | BindingFlags.Public);
        Assert.IsNotNull(property,
            rig.GetType().Name + " is missing " + propertyName + ".");
        return (string)property.GetValue(rig);
    }

    private static Transform ReadTarget(MonoBehaviour rig)
    {
        var property = rig.GetType().GetProperty(
            "CameraResolvedTarget", BindingFlags.Instance | BindingFlags.Public);
        Assert.IsNotNull(property,
            rig.GetType().Name + " does not expose CameraResolvedTarget.");
        Assert.AreEqual(typeof(Transform), property.PropertyType);
        return (Transform)property.GetValue(rig);
    }

    private static IEnumerator ClearFixtureObjects()
    {
        foreach (var camera in UnityEngine.Object.FindObjectsByType<Camera>())
            UnityEngine.Object.Destroy(camera.gameObject);
        var player = GameObject.Find("Player");
        if (player != null) UnityEngine.Object.Destroy(player);
        var sideGround = GameObject.Find("FixtureSideGround");
        if (sideGround != null) UnityEngine.Object.Destroy(sideGround);
        yield return null;
    }

    private static MonoBehaviour AttachSideScrollerController(Transform player)
    {
        // The 2D fixture is not only an orthographic camera: it must exercise
        // the compositor-selected 2d_platformer controller. This generated
        // test controller owns the Rigidbody2D and BoxCollider2D prerequisites.
        var controller = (MonoBehaviour)player.gameObject.AddComponent(
            FindFixtureType("FixtureSideController"));
        Assert.IsNotNull(player.GetComponent<Rigidbody2D>(),
            "The 2D side controller did not create its Rigidbody2D.");
        Assert.IsNotNull(player.GetComponent<Collider2D>(),
            "The 2D side controller did not create its Collider2D.");
        return controller;
    }

    private static void CreateSideGround()
    {
        var ground = new GameObject("FixtureSideGround");
        ground.transform.position = new Vector3(0f, -1.05f, 0f);
        var collider = ground.AddComponent<BoxCollider2D>();
        collider.size = new Vector2(30f, 0.5f);
    }

    private static Transform CreatePlayer(out Transform eye, out Transform focus)
    {
        var player = new GameObject("Player");
        player.tag = "Player";
        eye = new GameObject("Eye").transform;
        eye.SetParent(player.transform, false);
        eye.localPosition = new Vector3(0f, 1.6f, 0f);
        focus = new GameObject("CameraFocus").transform;
        focus.SetParent(player.transform, false);
        // The focus is deliberately in front of the player. A camera that
        // accidentally aims at its placement offset or root will fail angle
        // and viewport assertions rather than passing by coincidence.
        focus.localPosition = new Vector3(0f, 1.1f, 3f);
        return player.transform;
    }

    private static Camera CreateMainCamera(out GameObject cameraObject)
    {
        cameraObject = new GameObject("Main Camera");
        cameraObject.tag = "MainCamera";
        return cameraObject.AddComponent<Camera>();
    }

    private static void AssertFramed(Camera camera, Transform target, string mode,
                                     bool requiresLookAt = true)
    {
        Vector3 targetVector = target.position - camera.transform.position;
        Assert.Greater(targetVector.sqrMagnitude, 0.0001f,
            mode + " target overlaps the camera.");
        if (requiresLookAt)
            Assert.LessOrEqual(Vector3.Angle(camera.transform.forward, targetVector), 2f,
                mode + " is not aimed at its resolved target.");
        Vector3 viewport = camera.WorldToViewportPoint(target.position);
        Assert.Greater(viewport.z, 0f, mode + " target is behind Main Camera.");
        Assert.That(viewport.x, Is.InRange(0.05f, 0.95f),
            mode + " target is outside the horizontal frame.");
        Assert.That(viewport.y, Is.InRange(0.05f, 0.95f),
            mode + " target is outside the vertical frame.");
    }

    [UnityTest]
    public IEnumerator EveryCameraFixtureAttachesToMainCameraAndMeetsItsContract()
    {
        foreach (var fixture in Fixtures)
        {
            yield return ClearFixtureObjects();
            var player = CreatePlayer(out var eye, out var focus);
            MonoBehaviour sideController = null;
            if (fixture.IsTwoD)
            {
                CreateSideGround();
                sideController = AttachSideScrollerController(player);
            }
            var camera = CreateMainCamera(out var cameraObject);
            var rig = (MonoBehaviour)cameraObject.AddComponent(
                FindFixtureType(fixture.ClassName));

            // Allow Awake plus the generated LateUpdate follow loop to settle.
            yield return null;
            yield return null;
            yield return null;
            yield return null;
            // SmoothDamp uses elapsed time, not a fixed number of frames. A
            // freshly-created 2D camera starts at z=0 and needs real Play
            // Mode time to reach its contract-owned fixed depth at z=-10.
            yield return new WaitForSeconds(0.5f);

            Assert.AreSame(camera, Camera.main,
                fixture.ClassName + " was not attached to Main Camera.");
            Assert.AreEqual(1, camera.GetComponents<MonoBehaviour>().Length,
                fixture.ClassName + " must be the only contract owner on Main Camera.");
            Assert.AreEqual(fixture.Mode, ReadString(rig, "CameraModeID"));
            Assert.AreEqual(fixture.TargetType, ReadString(rig, "CameraPrimaryTargetType"));
            Assert.AreEqual(fixture.TargetRef, ReadString(rig, "CameraPrimaryTargetRef"));

            Transform target = ReadTarget(rig);
            Assert.IsNotNull(target, fixture.ClassName + " did not resolve a target.");
            if (fixture.IsFirstPerson)
            {
                Assert.AreSame(eye, target);
                Assert.Less(Vector3.Distance(camera.transform.position, eye.position), 0.01f);
                Assert.Less(Quaternion.Angle(camera.transform.rotation, eye.rotation), 0.5f);
            }
            else if (fixture.IsTwoD)
            {
                Assert.IsNotNull(sideController,
                    "Follow2D requires the generated 2D side controller.");
                Assert.AreSame(player, target);
                Assert.IsTrue(camera.orthographic);
                Assert.Less(Quaternion.Angle(camera.transform.rotation, Quaternion.identity), 0.5f);
                AssertFramed(camera, target, fixture.Mode, requiresLookAt: false);
            }
            else
            {
                Assert.AreSame(focus, target);
                AssertFramed(camera, target, fixture.Mode);
            }

            Vector3 before = camera.transform.position;
            player.position += Vector3.right;
            yield return new WaitForSeconds(0.25f);
            Assert.Greater(Vector3.Distance(before, camera.transform.position), 0.001f,
                fixture.Mode + " did not follow its player anchor.");

            Debug.Log($"CAMERA_EVIDENCE fixture={fixture.ClassName} mode={fixture.Mode} "
                + $"target={target.name} camera={camera.transform.position} "
                + $"forward={camera.transform.forward}");
        }
        yield return ClearFixtureObjects();
    }
}
