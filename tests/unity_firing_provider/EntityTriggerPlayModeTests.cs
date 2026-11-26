using System.Collections;
using System.Collections.Generic;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.TestTools;

/// <summary>Entity trigger acceptance: a canned collect sequence runs on Player
/// contact, never at spawn, and its Despawn removes its own host. This test is
/// hand-written. The class it drives, CoinBehavior, is emitted by
/// tools/generate_firing_provider_acceptance.py from EntityBehavior.cs.j2 for
/// the canned `collect` atoms (behavior_map.behavior_trigger: player_contact).
/// Each coin is built in EntitySpawner's shape: a primitive with its collider,
/// named "&lt;group&gt;_&lt;i&gt;", with the behavior added last.
///
/// The movers are the two player bodies the controller micro builds: a dynamic
/// Rigidbody (vehicles, aircraft) and a CharacterController (walkers). A
/// kinematic Rigidbody is not one of them, and under the default
/// Physics.contactPairsMode a kinematic body against a static trigger reports
/// no trigger at all. Each case advances real Play Mode frames and asserts the
/// frame count moved.</summary>
public sealed class EntityTriggerPlayModeTests
{
    // Long enough for Start() and many Updates: a sequence started from Start
    // would have destroyed the coin within the first two frames.
    private const int UntouchedFrames = 30;
    private static readonly Vector3 MoverStart = new Vector3(6f, 0f, 0f);

    private readonly List<GameObject> _spawned = new List<GameObject>();

    [SetUp]
    public void SetUp()
    {
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

    private GameObject SpawnCoin()
    {
        var coin = GameObject.CreatePrimitive(PrimitiveType.Cube);
        coin.name = "Coin_0";
        coin.transform.position = Vector3.zero;
        _spawned.Add(coin);
        coin.AddComponent<CoinBehavior>();
        return coin;
    }

    /// <summary>A dynamic body with gravity off, as a vehicle or aircraft
    /// controller builds the Player.</summary>
    private GameObject SpawnRigidbodyMover(string tag)
    {
        var mover = GameObject.CreatePrimitive(PrimitiveType.Sphere);
        mover.name = "RigidbodyMover";
        mover.tag = tag;
        mover.transform.position = MoverStart;
        var body = mover.AddComponent<Rigidbody>();
        body.useGravity = false;
        body.constraints = RigidbodyConstraints.FreezeRotation;
        _spawned.Add(mover);
        return mover;
    }

    /// <summary>A CharacterController, as a walking controller builds the Player.</summary>
    private GameObject SpawnCharacterMover(string tag)
    {
        var mover = new GameObject("CharacterMover") { tag = tag };
        mover.transform.position = MoverStart;
        var controller = mover.AddComponent<CharacterController>();
        controller.radius = 0.4f;
        controller.height = 1.6f;
        _spawned.Add(mover);
        return mover;
    }

    private static IEnumerator MoveOnto(GameObject mover)
    {
        var body = mover.GetComponent<Rigidbody>();
        var controller = mover.GetComponent<CharacterController>();
        for (int step = 1; step <= 6; step++)
        {
            Vector3 next = Vector3.Lerp(MoverStart, Vector3.zero, step / 6f);
            if (controller != null) controller.Move(next - mover.transform.position);
            else body.MovePosition(next);
            yield return new WaitForFixedUpdate();
        }
        // A trigger pair is evaluated by a physics step, and the last move
        // lands after one. A batchmode frame lasts microseconds, so a few frames
        // run no fixed step at all: wait for two, with the mover inside.
        yield return new WaitForFixedUpdate();
        yield return new WaitForFixedUpdate();
        // The contact starts the sequence; its LightFlash finds no Light, so
        // it reaches Despawn in the same frame. Destroy lands at frame end.
        yield return null;
        yield return null;
    }

    private IEnumerator ContactCollects(GameObject player, string marker)
    {
        int startFrame = Time.frameCount;
        GameObject coin = SpawnCoin();
        yield return null;
        yield return null;
        Assert.IsTrue(coin != null, "The coin despawned before any contact.");

        yield return MoveOnto(player);

        Assert.IsTrue(coin == null, "A Player-tagged " + player.name + " did not collect the coin.");
        Assert.IsTrue(player != null, "Despawn self removed something other than its host.");
        Assert.Greater(Time.frameCount, startFrame, "Play Mode frames did not advance.");
        Debug.Log(marker + " despawned=Coin_0 frameAdvance=" + (Time.frameCount - startFrame));
    }

    [UnityTest]
    public IEnumerator AnUntouchedPickupSurvivesItsSpawn()
    {
        int startFrame = Time.frameCount;
        GameObject coin = SpawnCoin();
        for (int frame = 0; frame < UntouchedFrames; frame++)
            yield return null;

        Assert.IsTrue(coin != null, "CoinBehavior ran its collect sequence without a contact.");
        Assert.IsTrue(coin.GetComponent<Collider>().isTrigger,
                      "CoinBehavior.Start did not make the coin's collider a trigger.");
        Assert.Greater(Time.frameCount, startFrame, "Play Mode frames did not advance.");
        Debug.Log("ENTITY_TRIGGER_UNTOUCHED_PASS frames=" + UntouchedFrames
                  + " frameAdvance=" + (Time.frameCount - startFrame));
    }

    [UnityTest]
    public IEnumerator ARigidbodyPlayerContactCollectsThePickup()
    {
        yield return ContactCollects(SpawnRigidbodyMover("Player"),
                                     "ENTITY_TRIGGER_RIGIDBODY_CONTACT_PASS");
    }

    [UnityTest]
    public IEnumerator ACharacterControllerPlayerContactCollectsThePickup()
    {
        yield return ContactCollects(SpawnCharacterMover("Player"),
                                     "ENTITY_TRIGGER_CHARACTER_CONTACT_PASS");
    }

    [UnityTest]
    public IEnumerator AnUntaggedContactLeavesThePickup()
    {
        int startFrame = Time.frameCount;
        GameObject coin = SpawnCoin();
        GameObject crate = SpawnRigidbodyMover("Untagged");
        yield return null;
        yield return null;

        yield return MoveOnto(crate);

        Assert.IsTrue(coin != null, "A contact from a non-Player collider collected the coin.");
        Assert.Greater(Time.frameCount, startFrame, "Play Mode frames did not advance.");
        Debug.Log("ENTITY_TRIGGER_UNTAGGED_PASS kept=Coin_0 frameAdvance="
                  + (Time.frameCount - startFrame));
    }
}
