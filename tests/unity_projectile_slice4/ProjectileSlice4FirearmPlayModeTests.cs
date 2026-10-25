using System.Collections;
using System.Collections.Generic;
using System.Reflection;
using NUnit.Framework;
using UnityEditor;
using UnityEditor.Animations;
using UnityEngine;
using UnityEngine.TestTools;

public sealed class Slice4DamageProbe : MonoBehaviour
{
    public int HitCount { get; private set; }
    public float TotalDamage { get; private set; }

    public void TakeDamage(float amount)
    {
        HitCount++;
        TotalDamage += amount;
    }
}

public sealed class ProjectileSlice4FirearmPlayModeTests
{
    private static readonly BindingFlags PrivateInstance =
        BindingFlags.Instance | BindingFlags.NonPublic;

    [UnityTest]
    public IEnumerator HeldFireFallbackProducesManyVisibleLaunchesAndImpacts()
    {
        var spawned = new List<GameObject>();
        ProjectileSlice4Acceptance controller = BuildFixture(
            spawned, null, out Transform weapon, out Slice4DamageProbe target);
        MethodInfo processFire = typeof(ProjectileSlice4Acceptance).GetMethod(
            "ProcessFireInput", PrivateInstance);
        Assert.That(processFire, Is.Not.Null);

        yield return null;
        int initialAmmo = controller.CurrentAmmo;
        float restZ = weapon.localPosition.z;
        float deadline = Time.time + 1.20f;
        bool sawVisibleProjectile = false;
        bool sawGeneratedRecoil = false;
        while (Time.time < deadline)
        {
            processFire.Invoke(controller, new object[] { true });
            sawGeneratedRecoil |= weapon.localPosition.z < restZ - 0.001f;
            foreach (ProjectileController projectile in
                     Resources.FindObjectsOfTypeAll<ProjectileController>())
            {
                if (!projectile.gameObject.scene.IsValid()
                    || !projectile.gameObject.activeInHierarchy) continue;
                Renderer renderer = projectile.GetComponentInChildren<Renderer>(true);
                Rigidbody body = projectile.GetComponent<Rigidbody>();
                if (renderer != null && renderer.enabled && body != null && !body.isKinematic)
                    sawVisibleProjectile = true;
            }
            yield return null;
        }

        float hitDeadline = Time.time + 1f;
        while (target.HitCount < 5 && Time.time < hitDeadline)
            yield return new WaitForFixedUpdate();

        int launches = controller.ProjectileLaunchCount;
        Assert.That(launches, Is.GreaterThanOrEqualTo(5));
        Assert.That(controller.CurrentAmmo, Is.EqualTo(initialAmmo - launches));
        Assert.That(controller.PendingProjectileReleases, Is.EqualTo(0));
        Assert.That(target.HitCount, Is.GreaterThanOrEqualTo(5));
        Assert.That(target.HitCount, Is.LessThanOrEqualTo(launches));
        Assert.That(target.TotalDamage, Is.EqualTo(target.HitCount * 25f).Within(0.001f));
        Assert.That(sawVisibleProjectile, Is.True);
        Assert.That(sawGeneratedRecoil, Is.True);
        Assert.That(controller.ResolvedProjectileMuzzleName, Is.EqualTo("Muzzle"));
        Assert.That(FindDescendant(controller.transform, "GeneratedMuzzleFlash"), Is.Not.Null);

        Debug.Log(
            "SLICE4_AUTOMATIC_PASS held_input=true launches=" + launches
            + " ammo_decrements=" + (initialAmmo - controller.CurrentAmmo)
            + " impacts=" + target.HitCount
            + " visible_projectile=" + sawVisibleProjectile
            + " generated_recoil=" + sawGeneratedRecoil);
        DestroyAll(spawned);
    }

    [UnityTest]
    public IEnumerator CompatibleAnimatorReleasesOnNamedAnimationEventExactlyOnce()
    {
        const string assetRoot = "Assets/Tests/GeneratedSlice4";
        AssetDatabase.DeleteAsset(assetRoot);
        AssetDatabase.CreateFolder("Assets/Tests", "GeneratedSlice4");
        RuntimeAnimatorController runtimeController = CreateFireAnimator(assetRoot);

        var spawned = new List<GameObject>();
        ProjectileSlice4Acceptance controller = BuildFixture(
            spawned, runtimeController, out _, out Slice4DamageProbe target);
        MethodInfo tryShoot = typeof(ProjectileSlice4Acceptance).GetMethod(
            "TryShoot", PrivateInstance);
        Assert.That(tryShoot, Is.Not.Null);

        yield return null;
        int initialAmmo = controller.CurrentAmmo;
        tryShoot.Invoke(controller, null);

        Assert.That(controller.UsesAnimatorFireRelease, Is.True);
        Assert.That(controller.CurrentAmmo, Is.EqualTo(initialAmmo - 1));
        Assert.That(controller.PendingProjectileReleases, Is.EqualTo(1));
        Assert.That(controller.ProjectileLaunchCount, Is.EqualTo(0));

        float releaseDeadline = Time.time + 0.11f;
        while (controller.ProjectileLaunchCount == 0 && Time.time < releaseDeadline)
            yield return null;
        Assert.That(controller.ProjectileLaunchCount, Is.EqualTo(1));
        Assert.That(controller.PendingProjectileReleases, Is.EqualTo(0));

        float hitDeadline = Time.time + 0.5f;
        while (target.HitCount == 0 && Time.time < hitDeadline)
            yield return new WaitForFixedUpdate();
        Assert.That(target.HitCount, Is.EqualTo(1));
        Assert.That(controller.CurrentAmmo, Is.EqualTo(initialAmmo - 1));

        controller.OnFirearmRelease();
        Assert.That(controller.ProjectileLaunchCount, Is.EqualTo(1));
        Assert.That(controller.CurrentAmmo, Is.EqualTo(initialAmmo - 1));

        Debug.Log(
            "SLICE4_ANIM_EVENT_PASS event=OnFirearmRelease launches=1 "
            + "ammo_decrements=1 impacts=" + target.HitCount
            + " duplicate_event=noop");
        DestroyAll(spawned);
        AssetDatabase.DeleteAsset(assetRoot);
    }

    private static ProjectileSlice4Acceptance BuildFixture(
        ICollection<GameObject> spawned,
        RuntimeAnimatorController runtimeController,
        out Transform weapon,
        out Slice4DamageProbe targetProbe)
    {
        var host = new GameObject("Slice4ControllerHost");
        spawned.Add(host);
        host.SetActive(false);

        var eye = new GameObject("Eye");
        spawned.Add(eye);
        eye.transform.SetParent(host.transform, false);
        eye.transform.localPosition = new Vector3(0f, 1.4f, 0f);

        var weaponObject = GameObject.CreatePrimitive(PrimitiveType.Cube);
        spawned.Add(weaponObject);
        weaponObject.name = "PlayerWeapon";
        weaponObject.transform.SetParent(eye.transform, false);
        weaponObject.transform.localPosition = new Vector3(0.28f, -0.22f, 0.52f);
        weaponObject.transform.localScale = new Vector3(0.16f, 0.12f, 0.62f);
        weaponObject.GetComponent<Collider>().enabled = false;
        weapon = weaponObject.transform;

        var muzzle = new GameObject("Muzzle");
        spawned.Add(muzzle);
        muzzle.transform.SetParent(weapon, false);
        muzzle.transform.localPosition = new Vector3(0f, 0f, 0.42f);

        if (runtimeController != null)
        {
            Animator animator = host.AddComponent<Animator>();
            animator.runtimeAnimatorController = runtimeController;
        }
        var controller = host.AddComponent<ProjectileSlice4Acceptance>();

        var cameraObject = new GameObject("Slice4Camera");
        spawned.Add(cameraObject);
        cameraObject.tag = "MainCamera";
        cameraObject.transform.SetPositionAndRotation(
            new Vector3(0f, 1.4f, 0f), Quaternion.identity);
        cameraObject.AddComponent<Camera>();

        GameObject ground = GameObject.CreatePrimitive(PrimitiveType.Cube);
        spawned.Add(ground);
        ground.name = "Slice4Ground";
        ground.transform.position = new Vector3(0f, -0.55f, 3.5f);
        ground.transform.localScale = new Vector3(12f, 1f, 16f);

        GameObject target = GameObject.CreatePrimitive(PrimitiveType.Cube);
        spawned.Add(target);
        target.name = "Slice4ThinTarget";
        target.transform.position = new Vector3(0f, 1.4f, 7f);
        target.transform.localScale = new Vector3(5f, 5f, 0.04f);
        targetProbe = target.AddComponent<Slice4DamageProbe>();

        host.SetActive(true);
        Physics.SyncTransforms();
        return controller;
    }

    private static RuntimeAnimatorController CreateFireAnimator(string assetRoot)
    {
        var idle = new AnimationClip { name = "Idle" };
        idle.SetCurve(
            "", typeof(Transform), "m_LocalRotation.w",
            AnimationCurve.Constant(0f, 1f, 1f));
        AssetDatabase.CreateAsset(idle, assetRoot + "/Idle.anim");

        var fire = new AnimationClip { name = "Fire" };
        fire.SetCurve(
            "Eye/PlayerWeapon", typeof(Transform), "m_LocalPosition.z",
            new AnimationCurve(
                new Keyframe(0f, 0.52f),
                new Keyframe(0.04f, 0.46f),
                new Keyframe(0.10f, 0.52f)));
        fire.AddEvent(new AnimationEvent
        {
            functionName = "OnFirearmRelease",
            time = 0.04f,
        });
        AssetDatabase.CreateAsset(fire, assetRoot + "/Fire.anim");

        AnimatorController controller = AnimatorController.CreateAnimatorControllerAtPath(
            assetRoot + "/Firearm.controller");
        controller.AddParameter("Attack", AnimatorControllerParameterType.Trigger);
        AnimatorStateMachine machine = controller.layers[0].stateMachine;
        AnimatorState idleState = machine.AddState("Idle");
        idleState.motion = idle;
        machine.defaultState = idleState;
        AnimatorState fireState = machine.AddState("Fire");
        fireState.motion = fire;
        AnimatorStateTransition enter = machine.AddAnyStateTransition(fireState);
        enter.hasExitTime = false;
        enter.duration = 0f;
        enter.canTransitionToSelf = false;
        enter.AddCondition(AnimatorConditionMode.If, 0f, "Attack");
        AnimatorStateTransition exit = fireState.AddTransition(idleState);
        exit.hasExitTime = true;
        exit.exitTime = 1f;
        exit.duration = 0f;
        AssetDatabase.SaveAssets();
        return controller;
    }

    private static Transform FindDescendant(Transform root, string name)
    {
        if (root.name == name) return root;
        for (int i = 0; i < root.childCount; i++)
        {
            Transform found = FindDescendant(root.GetChild(i), name);
            if (found != null) return found;
        }
        return null;
    }

    private static void DestroyAll(IEnumerable<GameObject> spawned)
    {
        foreach (GameObject instance in spawned)
            if (instance != null) Object.Destroy(instance);
    }
}
