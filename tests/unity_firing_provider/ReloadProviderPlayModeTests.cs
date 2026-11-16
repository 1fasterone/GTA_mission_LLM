using System.Collections;
using System.Collections.Generic;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.TestTools;

/// <summary>R4 (S6) acceptance: the Reload atom and the reload input share one
/// firing-provider gate (IFiringProvider.TryReload) in every controller variant
/// that implements the provider. This test is hand-written; every runtime class
/// it drives (one Reload* shooter per firing variant, FiringProvider and the
/// ArmoryBehavior that hosts the lowered Reload atom) is emitted by
/// tools/generate_firing_provider_acceptance.py from production templates.
/// Each case advances real Play Mode frames and asserts the frame count moved.</summary>
public sealed class ReloadProviderPlayModeTests
{
    private const string RefusalPrefix = "[atom.Reload] the firing provider refused the reload: ";
    private const string NoProviderError =
        "[atom.Reload] no IFiringProvider on the Player: compose a controller with the shoot ability";

    /// <summary>One shooter per variant that implements IFiringProvider. The
    /// Python suite (tests/test_reload_provider.py) pins this list to the
    /// acceptance generator's reload_variants(), so a variant cannot be added
    /// to one and not the other.</summary>
    private static readonly string[] Shooters =
    {
        "ReloadFirstPerson", "ReloadThirdPerson", "ReloadTopDown",
        "Reload2dPlatformer", "Reload2dTopdown", "Reload2dFighting", "ReloadPlatformer25d",
        "ReloadFlying", "ReloadSwimming", "ReloadClimbing", "ReloadAircraft", "ReloadFighting",
    };

    private readonly List<GameObject> _spawned = new List<GameObject>();

    [SetUp]
    public void SetUp()
    {
        // Hit-scan aims along the shooter's own forward without Camera.main.
        foreach (Camera sceneCamera in Object.FindObjectsByType<Camera>(FindObjectsSortMode.None))
            sceneCamera.tag = "Untagged";
        Assert.IsNull(GameObject.FindWithTag("Player"), "A previous Player survived teardown.");
    }

    [UnityTearDown]
    public IEnumerator TearDown()
    {
        foreach (GameObject instance in _spawned)
            if (instance != null) Object.Destroy(instance);
        _spawned.Clear();
        yield return null;
    }

    private MonoBehaviour SpawnShooter(string shooterClass)
    {
        System.Type type = System.Type.GetType(shooterClass + ", FiringProvider.Runtime");
        Assert.IsNotNull(type, "The acceptance generator emitted no " + shooterClass + ".");
        var player = new GameObject("Player") { tag = "Player" };
        _spawned.Add(player);
        // Awake runs here: magazine, reserve and the variant's own body.
        var shooter = (MonoBehaviour)player.AddComponent(type);
        // Update reads hardware input, which batchmode cannot drive. The test
        // calls the gates exactly as the Update input and the atoms do.
        shooter.enabled = false;
        return shooter;
    }

    /// <summary>Runs the lowered Reload atom through its real host: the
    /// generated ArmoryBehavior runs entities[Armory].atoms ([Reload, Despawn])
    /// once from Start, as EntitySpawner's AddComponent would, then despawns.</summary>
    private IEnumerator RunReloadAtom()
    {
        var host = new GameObject("Armory");
        _spawned.Add(host);
        host.AddComponent<ArmoryBehavior>();
        yield return null;
        yield return null;
    }

    private static void EmptyMagazine(IFiringProvider provider)
    {
        while (provider.CurrentAmmo > 0)
            Assert.AreEqual(FireResult.Fired, FiringProviders.FirePlayer());
    }

    [UnityTest]
    public IEnumerator ReloadAtomAndInputShareOneGate(
        [ValueSource(nameof(Shooters))] string shooterClass)
    {
        int startFrame = Time.frameCount;
        MonoBehaviour shooter = SpawnShooter(shooterClass);
        IFiringProvider provider = FiringProviders.ForPlayer();
        Assert.AreSame(shooter, provider, "The Player's provider is not the " + shooterClass + ".");
        yield return null;
        Assert.AreEqual(2, provider.CurrentAmmo);
        Assert.AreEqual(3, provider.ReserveAmmo);

        // A full magazine refuses through the provider and the atom alike.
        Assert.AreEqual(ReloadResult.MagazineFull, FiringProviders.ReloadPlayer());
        LogAssert.Expect(LogType.Warning, RefusalPrefix + ReloadResult.MagazineFull);
        yield return RunReloadAtom();
        Assert.IsFalse(provider.IsReloading);
        Assert.AreEqual(3, provider.ReserveAmmo);

        // The atom starts a reload through the one gate...
        EmptyMagazine(provider);
        yield return RunReloadAtom();
        Assert.IsTrue(provider.IsReloading, "The Reload atom did not start a reload in " + shooterClass + ".");
        // ...which both gates then honour: no second reload, no shot, nothing moved.
        Assert.AreEqual(ReloadResult.AlreadyReloading, provider.TryReload());
        Assert.AreEqual(FireResult.Reloading, FiringProviders.FirePlayer());
        Assert.AreEqual(0, provider.CurrentAmmo);
        Assert.AreEqual(3, provider.ReserveAmmo);
        yield return new WaitForSeconds(1.3f);
        Assert.IsFalse(provider.IsReloading);
        Assert.AreEqual(2, provider.CurrentAmmo);
        Assert.AreEqual(1, provider.ReserveAmmo);

        // The reload input's gate draws the last reserve round.
        EmptyMagazine(provider);
        Assert.AreEqual(ReloadResult.Started, provider.TryReload());
        yield return new WaitForSeconds(1.3f);
        Assert.AreEqual(1, provider.CurrentAmmo);
        Assert.AreEqual(0, provider.ReserveAmmo);

        // No reserve left: both refuse without starting a reload.
        EmptyMagazine(provider);
        Assert.AreEqual(ReloadResult.NoReserveAmmo, FiringProviders.ReloadPlayer());
        LogAssert.Expect(LogType.Warning, RefusalPrefix + ReloadResult.NoReserveAmmo);
        yield return RunReloadAtom();
        Assert.IsFalse(provider.IsReloading);
        Assert.AreEqual(0, provider.CurrentAmmo);
        Assert.AreEqual(FireResult.EmptyMagazine, FiringProviders.FirePlayer());

        Assert.Greater(Time.frameCount, startFrame, "Play Mode frames did not advance.");
        Debug.Log("RELOAD_PROVIDER_VARIANT_PASS shooter=" + shooterClass
                  + " results=MagazineFull,Started,AlreadyReloading,NoReserveAmmo"
                  + " frameAdvance=" + (Time.frameCount - startFrame));
    }

    [UnityTest]
    public IEnumerator NoProviderIsALoudReloadRefusal()
    {
        int startFrame = Time.frameCount;
        yield return null;
        Assert.IsNull(FiringProviders.ForPlayer());
        Assert.AreEqual(ReloadResult.NoProvider, FiringProviders.ReloadPlayer());
        LogAssert.Expect(LogType.Error, NoProviderError);
        yield return RunReloadAtom();

        Assert.Greater(Time.frameCount, startFrame, "Play Mode frames did not advance.");
        Debug.Log("RELOAD_PROVIDER_NO_PROVIDER_PASS refused=NoProvider frameAdvance="
                  + (Time.frameCount - startFrame));
    }
}
