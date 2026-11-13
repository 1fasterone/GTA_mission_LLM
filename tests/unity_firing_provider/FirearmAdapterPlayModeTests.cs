using System.Collections;
using System.Collections.Generic;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.TestTools;
#if UNITY_EDITOR
using UnityEditor;
using UnityEditor.Animations;
#endif

/// <summary>Slice 4 firearm adapter, ported onto the R3 firing provider.
/// Hand-written test; FirearmShooter is emitted from a fresh rifle brief's
/// closed fast_round contract by tools/generate_firing_provider_acceptance.py.
/// Every shot goes through FiringProviders.FirePlayer(), the gate the fire
/// input and the Shoot atom share; the adapter only presents an accepted shot
/// and decides when its reserved round launches. The controller stays enabled
/// because its Update owns the release timeout and the recoil return.</summary>
public sealed class FirearmAdapterPlayModeTests
{
    private const string AssetRoot = "Assets/Tests/GeneratedFirearm";
    private const float ReleaseTimeout = 0.12f;
    private const float FastRoundDamage = 25f;

    private readonly List<GameObject> _spawned = new List<GameObject>();

    [SetUp]
    public void SetUp()
    {
        // Without Camera.main the round aims along its muzzle's forward.
        foreach (Camera sceneCamera in Object.FindObjectsByType<Camera>(FindObjectsSortMode.None))
            sceneCamera.tag = "Untagged";
        Assert.IsNull(Camera.main);
        Assert.IsNull(GameObject.FindWithTag("Player"), "A previous Player survived teardown.");
    }

    [UnityTearDown]
    public IEnumerator TearDown()
    {
        foreach (GameObject instance in _spawned)
            if (instance != null) Object.Destroy(instance);
        _spawned.Clear();
        foreach (ProjectileController round in
                 Object.FindObjectsByType<ProjectileController>(FindObjectsInactive.Include, FindObjectsSortMode.None))
            Object.Destroy(round.gameObject);
#if UNITY_EDITOR
        AssetDatabase.DeleteAsset(AssetRoot);
#endif
        yield return null;
    }

    private FirearmShooter SpawnShooter(RuntimeAnimatorController animatorController,
                                        out DamageCounter target)
    {
        GameObject ground = GameObject.CreatePrimitive(PrimitiveType.Plane);
        ground.transform.localScale = new Vector3(4f, 1f, 4f);
        _spawned.Add(ground);
        GameObject wall = GameObject.CreatePrimitive(PrimitiveType.Cube);
        wall.transform.position = new Vector3(0f, 1.5f, 8f);
        wall.transform.localScale = new Vector3(6f, 6f, 0.5f);
        target = wall.AddComponent<DamageCounter>();
        _spawned.Add(wall);
        // autoSyncTransforms is off; place the colliders before any shot.
        Physics.SyncTransforms();

        var player = new GameObject("Player") { tag = "Player" };
        player.transform.position = new Vector3(0f, 1.05f, 0f);
        _spawned.Add(player);
        if (animatorController != null)
        {
            // AnimationEvents reach components beside the Animator only.
            Animator animator = player.AddComponent<Animator>();
            animator.runtimeAnimatorController = animatorController;
        }
        return player.AddComponent<FirearmShooter>();
    }

    [UnityTest]
    public IEnumerator FallbackReleasesEveryAcceptedRoundExactlyOnce()
    {
        int startFrame = Time.frameCount;
        FirearmShooter shooter = SpawnShooter(null, out DamageCounter target);
        yield return null;
        int initialAmmo = shooter.CurrentAmmo;
        const int shots = 6;

        for (int i = 0; i < shots; i++)
        {
            Assert.AreEqual(FireResult.Fired, FiringProviders.FirePlayer(), "shot " + i);
            // No compatible Animator: the generated presentation releases now.
            Assert.AreEqual(i + 1, shooter.ProjectileLaunchCount);
            Assert.AreEqual(0, shooter.PendingProjectileReleases);
            Assert.AreEqual(initialAmmo - (i + 1), shooter.CurrentAmmo);
            // The adapter changed presentation, not acceptance: same cadence.
            Assert.AreEqual(FireResult.CoolingDown, FiringProviders.FirePlayer());
            Assert.AreEqual(i + 1, shooter.ProjectileLaunchCount);
            yield return new WaitForSeconds(0.25f);
        }

        float deadline = Time.time + 1f;
        while (target.Hits < shots && Time.time < deadline)
            yield return new WaitForFixedUpdate();
        Assert.GreaterOrEqual(target.Hits, shots - 1, "Released rounds did not reach the target.");
        Assert.LessOrEqual(target.Hits, shots);
        Assert.AreEqual(target.Hits * FastRoundDamage, target.Total, 0.001f);
        Assert.IsFalse(shooter.UsesAnimatorFireRelease);
        Assert.AreEqual("GeneratedProjectileMuzzle", shooter.ResolvedProjectileMuzzleName);
        Assert.IsNotNull(FindDescendant(shooter.transform, "GeneratedMuzzleFlash"));
        Assert.IsNotNull(FindDescendant(shooter.transform, "GeneratedFirearmPresentation"));

        Assert.Greater(Time.frameCount, startFrame, "Play Mode frames did not advance.");
        Debug.Log("FIREARM_FALLBACK_PASS shots=" + shots + " launches=" + shooter.ProjectileLaunchCount
                  + " ammoSpent=" + (initialAmmo - shooter.CurrentAmmo) + " hits=" + target.Hits
                  + " frameAdvance=" + (Time.frameCount - startFrame));
    }

#if UNITY_EDITOR
    [UnityTest]
    public IEnumerator CompatibleAnimatorReleasesOnTheNamedEventExactlyOnce()
    {
        int startFrame = Time.frameCount;
        FirearmShooter shooter = SpawnShooter(CreateFireAnimator(withReleaseEvent: true),
                                              out DamageCounter target);
        yield return null;
        int initialAmmo = shooter.CurrentAmmo;

        // Accepted by the gate: ammo and cadence are spent at reservation,
        // but the round waits for the Animator's release event.
        Assert.AreEqual(FireResult.Fired, FiringProviders.FirePlayer());
        Assert.IsTrue(shooter.UsesAnimatorFireRelease);
        Assert.AreEqual(initialAmmo - 1, shooter.CurrentAmmo);
        Assert.AreEqual(1, shooter.PendingProjectileReleases);
        Assert.AreEqual(0, shooter.ProjectileLaunchCount);

        // The event (0.04 s into the clip) must beat the 0.12 s fallback.
        float eventDeadline = Time.time + ReleaseTimeout - 0.01f;
        while (shooter.ProjectileLaunchCount == 0 && Time.time < eventDeadline)
            yield return null;
        Assert.AreEqual(1, shooter.ProjectileLaunchCount, "The release event did not launch the round.");
        Assert.AreEqual(0, shooter.PendingProjectileReleases);

        float hitDeadline = Time.time + 1f;
        while (target.Hits == 0 && Time.time < hitDeadline)
            yield return new WaitForFixedUpdate();
        Assert.AreEqual(1, target.Hits);
        Assert.AreEqual(initialAmmo - 1, shooter.CurrentAmmo);

        // A duplicate or stale event is a no-op: the reservation queue is empty.
        shooter.OnFirearmRelease();
        Assert.AreEqual(1, shooter.ProjectileLaunchCount);
        Assert.AreEqual(initialAmmo - 1, shooter.CurrentAmmo);

        Assert.Greater(Time.frameCount, startFrame, "Play Mode frames did not advance.");
        Debug.Log("FIREARM_ANIM_EVENT_PASS event=OnFirearmRelease launches=1 ammoSpent=1 hits="
                  + target.Hits + " duplicateEvent=noop frameAdvance=" + (Time.frameCount - startFrame));
    }

    [UnityTest]
    public IEnumerator IncompatibleAnimatorFallsBackWithoutLosingTheRound()
    {
        int startFrame = Time.frameCount;
        FirearmShooter shooter = SpawnShooter(CreateFireAnimator(withReleaseEvent: false),
                                              out DamageCounter target);
        yield return null;
        int initialAmmo = shooter.CurrentAmmo;

        // A trigger but no release event: never wait on an event that cannot come.
        Assert.AreEqual(FireResult.Fired, FiringProviders.FirePlayer());
        Assert.IsFalse(shooter.UsesAnimatorFireRelease);
        Assert.AreEqual(1, shooter.ProjectileLaunchCount);
        Assert.AreEqual(0, shooter.PendingProjectileReleases);
        Assert.AreEqual(initialAmmo - 1, shooter.CurrentAmmo);

        float hitDeadline = Time.time + 1f;
        while (target.Hits == 0 && Time.time < hitDeadline)
            yield return new WaitForFixedUpdate();
        Assert.AreEqual(1, target.Hits);

        Assert.Greater(Time.frameCount, startFrame, "Play Mode frames did not advance.");
        Debug.Log("FIREARM_INCOMPATIBLE_PASS launches=1 ammoSpent=1 hits=" + target.Hits
                  + " frameAdvance=" + (Time.frameCount - startFrame));
    }

    private static RuntimeAnimatorController CreateFireAnimator(bool withReleaseEvent)
    {
        AssetDatabase.DeleteAsset(AssetRoot);
        AssetDatabase.CreateFolder("Assets/Tests", "GeneratedFirearm");

        var idle = new AnimationClip { name = "Idle" };
        idle.SetCurve("", typeof(Transform), "m_LocalRotation.w",
                      AnimationCurve.Constant(0f, 1f, 1f));
        AssetDatabase.CreateAsset(idle, AssetRoot + "/Idle.anim");

        var fire = new AnimationClip { name = "Fire" };
        fire.SetCurve("GeneratedFirearmPresentation", typeof(Transform), "m_LocalPosition.z",
                      new AnimationCurve(new Keyframe(0f, 0.52f), new Keyframe(0.04f, 0.46f),
                                         new Keyframe(0.10f, 0.52f)));
        if (withReleaseEvent)
            fire.AddEvent(new AnimationEvent { functionName = "OnFirearmRelease", time = 0.04f });
        AssetDatabase.CreateAsset(fire, AssetRoot + "/Fire.anim");

        AnimatorController controller =
            AnimatorController.CreateAnimatorControllerAtPath(AssetRoot + "/Firearm.controller");
        // "Attack" is the second trigger candidate, so resolution walks the list.
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
#endif

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
}
