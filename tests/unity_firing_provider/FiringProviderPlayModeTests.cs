using System.Collections;
using System.Collections.Generic;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.TestTools;

/// <summary>R3 (S5) acceptance: the Shoot atom and the controller share one
/// firing provider. This test is hand-written; every runtime class it drives
/// (HitscanShooter, BallisticShooter, FiringProvider, GameSequencer) is emitted
/// by tools/generate_firing_provider_acceptance.py from production templates.
/// Each case advances real Play Mode frames and asserts the frame count moved.</summary>
public sealed class FiringProviderPlayModeTests
{
    private const string RefusalPrefix = "[atom.Shoot] the firing provider refused the shot: ";
    private const string NoProviderError =
        "[atom.Shoot] no IFiringProvider on the Player: compose a controller with the shoot ability";

    private readonly List<GameObject> _spawned = new List<GameObject>();

    /// <summary>Hit-scan aims along the shooter's own forward when there is
    /// no Camera.main (TryShoot's documented branch); untag any scene camera
    /// so the ray cannot start behind the shooter and strike its own body.</summary>
    [SetUp]
    public void SetUp()
    {
        foreach (Camera sceneCamera in Object.FindObjectsByType<Camera>(FindObjectsSortMode.None))
            sceneCamera.tag = "Untagged";
        Assert.IsNull(Camera.main, "A scene camera would redirect the hit-scan ray.");
        Assert.IsNull(GameObject.FindWithTag("Player"), "A previous Player survived teardown.");
    }

    [UnityTearDown]
    public IEnumerator TearDown()
    {
        foreach (GameObject instance in _spawned)
            if (instance != null) Object.Destroy(instance);
        _spawned.Clear();
        // Borrowed rounds are unparented from their pool while in flight.
        foreach (ProjectileController round in
                 Object.FindObjectsByType<ProjectileController>(FindObjectsInactive.Include, FindObjectsSortMode.None))
            Object.Destroy(round.gameObject);
        yield return null;
    }

    private T SpawnShooter<T>() where T : MonoBehaviour
    {
        var player = new GameObject("Player") { tag = "Player" };
        _spawned.Add(player);
        // Awake runs here: magazine, reserve, pools, Eye, CharacterController.
        T shooter = player.AddComponent<T>();
        // Update reads hardware input, which batchmode cannot drive. The test
        // calls the provider exactly as the Update fire input and the Shoot
        // atom do (IFiringProvider.TryFire), so the shooter stays put.
        shooter.enabled = false;
        return shooter;
    }

    /// <summary>Runs the lowered Shoot atom through its real host: the
    /// generated GameSequencer fires schema.atoms once, 0.1 s after Start.</summary>
    private IEnumerator RunShootAtom()
    {
        var host = new GameObject("_GameSequencer");
        _spawned.Add(host);
        host.AddComponent<GameSequencer>();
        yield return new WaitForSeconds(0.25f);
    }

    [UnityTest]
    public IEnumerator AtomAndProviderShareAmmoCadenceAndReload()
    {
        int startFrame = Time.frameCount;
        var target = GameObject.CreatePrimitive(PrimitiveType.Cube);
        _spawned.Add(target);
        target.transform.position = new Vector3(0f, 0f, 5f);
        target.transform.localScale = new Vector3(4f, 4f, 0.5f);
        DamageCounter hits = target.AddComponent<DamageCounter>();
        // Physics.autoSyncTransforms is off and a batchmode frame need not run
        // a physics step, so the collider would still be a unit cube at the
        // origin (around the ray's start) when the first shot is cast.
        Physics.SyncTransforms();

        HitscanShooter shooter = SpawnShooter<HitscanShooter>();
        IFiringProvider provider = FiringProviders.ForPlayer();
        Assert.AreSame(shooter, provider, "The Player's provider is not the shooting controller.");
        yield return null;
        Assert.AreEqual(3, provider.CurrentAmmo);
        Assert.AreEqual(3, provider.ReserveAmmo);

        // A first shot is never cooling down: no shot has fired yet.
        Assert.AreEqual(FireResult.Fired, FiringProviders.FirePlayer());
        Assert.AreEqual(2, provider.CurrentAmmo);
        Assert.AreEqual(1, hits.Hits);

        // Inside fire_rate (1 s): the provider refuses, and so does the Shoot
        // atom, through the same cadence. Nothing is spent either time.
        Assert.AreEqual(FireResult.CoolingDown, FiringProviders.FirePlayer());
        LogAssert.Expect(LogType.Warning, RefusalPrefix + FireResult.CoolingDown);
        yield return RunShootAtom();
        Assert.AreEqual(2, provider.CurrentAmmo);
        Assert.AreEqual(1, hits.Hits);

        // Past fire_rate, the atom spends from the same magazine...
        yield return new WaitForSeconds(1.1f);
        yield return RunShootAtom();
        Assert.AreEqual(1, provider.CurrentAmmo, "The Shoot atom did not fire through the provider.");
        Assert.AreEqual(2, hits.Hits);

        // ...and its shot restarted the one cadence the provider enforces.
        Assert.AreEqual(FireResult.CoolingDown, FiringProviders.FirePlayer());
        yield return new WaitForSeconds(1.1f);
        Assert.AreEqual(FireResult.Fired, FiringProviders.FirePlayer());
        Assert.AreEqual(0, provider.CurrentAmmo);
        Assert.AreEqual(3, hits.Hits);

        // An empty magazine refuses both callers without spending anything.
        yield return new WaitForSeconds(1.1f);
        Assert.AreEqual(FireResult.EmptyMagazine, FiringProviders.FirePlayer());
        LogAssert.Expect(LogType.Warning, RefusalPrefix + FireResult.EmptyMagazine);
        yield return RunShootAtom();
        Assert.AreEqual(0, provider.CurrentAmmo);
        Assert.AreEqual(3, hits.Hits);

        // A reload in progress refuses; finishing it refills from reserve.
        shooter.StartCoroutine("ReloadRoutine");
        Assert.IsTrue(provider.IsReloading);
        Assert.AreEqual(FireResult.Reloading, FiringProviders.FirePlayer());
        yield return new WaitForSeconds(0.45f);
        Assert.IsFalse(provider.IsReloading);
        Assert.AreEqual(3, provider.CurrentAmmo);
        Assert.AreEqual(0, provider.ReserveAmmo);
        Assert.AreEqual(FireResult.Fired, FiringProviders.FirePlayer());
        Assert.AreEqual(2, provider.CurrentAmmo);
        Assert.AreEqual(4, hits.Hits);

        Assert.Greater(Time.frameCount, startFrame, "Play Mode frames did not advance.");
        Debug.Log("FIRING_PROVIDER_SHARED_GATE_PASS shots=4 refusals=CoolingDown,EmptyMagazine,Reloading"
                  + " atomShots=1 atomRefusals=CoolingDown,EmptyMagazine hits=" + hits.Hits
                  + " frameAdvance=" + (Time.frameCount - startFrame));
    }

    [UnityTest]
    public IEnumerator ExhaustedPoolRefusesWithoutSpendingAmmo()
    {
        int startFrame = Time.frameCount;
        BallisticShooter shooter = SpawnShooter<BallisticShooter>();
        IFiringProvider provider = FiringProviders.ForPlayer();
        Assert.AreSame(shooter, provider);
        yield return null;
        Assert.AreEqual(5, provider.CurrentAmmo);

        Assert.AreEqual(FireResult.Fired, FiringProviders.FirePlayer());
        Assert.AreEqual(4, provider.CurrentAmmo);
        // The fixed pool holds one round, still in flight (30 s lifetime).
        yield return new WaitForSeconds(0.15f);
        Assert.AreEqual(FireResult.NoProjectile, FiringProviders.FirePlayer());
        Assert.AreEqual(4, provider.CurrentAmmo, "A round that never launched spent ammunition.");
        LogAssert.Expect(LogType.Warning, RefusalPrefix + FireResult.NoProjectile);
        yield return RunShootAtom();
        Assert.AreEqual(4, provider.CurrentAmmo);

        Assert.Greater(Time.frameCount, startFrame, "Play Mode frames did not advance.");
        Debug.Log("FIRING_PROVIDER_POOL_PASS fired=1 refused=NoProjectile ammo=" + provider.CurrentAmmo
                  + " frameAdvance=" + (Time.frameCount - startFrame));
    }

    [UnityTest]
    public IEnumerator NoProviderIsALoudRefusal()
    {
        int startFrame = Time.frameCount;
        yield return null;
        Assert.IsNull(FiringProviders.ForPlayer());
        Assert.AreEqual(FireResult.NoProvider, FiringProviders.FirePlayer());
        LogAssert.Expect(LogType.Error, NoProviderError);
        yield return RunShootAtom();

        Assert.Greater(Time.frameCount, startFrame, "Play Mode frames did not advance.");
        Debug.Log("FIRING_PROVIDER_NO_PROVIDER_PASS refused=NoProvider frameAdvance="
                  + (Time.frameCount - startFrame));
    }
}

/// <summary>Test-side damage receiver: counts the TakeDamage messages a
/// hit-scan shot sends to what it strikes.</summary>
public sealed class DamageCounter : MonoBehaviour
{
    public int Hits { get; private set; }
    public float Total { get; private set; }

    private void TakeDamage(float amount)
    {
        Hits++;
        Total += amount;
    }
}
