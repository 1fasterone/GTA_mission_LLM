using System.Collections;
using System.Collections.Generic;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.TestTools;

public sealed class ProjectileDamageProbe : MonoBehaviour
{
    public int HitCount { get; private set; }
    public float TotalDamage { get; private set; }

    public void TakeDamage(float amount)
    {
        HitCount++;
        TotalDamage += amount;
    }
}

public sealed class ProjectileSweptImpactPlayModeTests
{
    private const float ProjectileSpeed = 600f;
    private const float ThinTargetDepth = 0.02f;

    [UnityTest]
    public IEnumerator FastProjectileCannotTunnelThroughThinStaticWall()
    {
        IEnumerator probe = RunSweepProbe(dynamicTarget: false);
        while (probe.MoveNext())
            yield return probe.Current;
    }

    [UnityTest]
    public IEnumerator FastProjectileCannotTunnelThroughDynamicTarget()
    {
        IEnumerator probe = RunSweepProbe(dynamicTarget: true);
        while (probe.MoveNext())
            yield return probe.Current;
    }

    private static IEnumerator RunSweepProbe(bool dynamicTarget)
    {
        float originalFixedDeltaTime = Time.fixedDeltaTime;
        var spawned = new List<GameObject>();
        Time.fixedDeltaTime = 0.02f;

        try
        {
            GameObject owner = CreateBox(
                "ProjectileOwner", Vector3.zero, new Vector3(1f, 1f, 1f), spawned);
            ProjectileDamageProbe ownerDamage = owner.AddComponent<ProjectileDamageProbe>();
            Collider ownerCollider = owner.GetComponent<Collider>();

            GameObject target = CreateBox(
                dynamicTarget ? "ThinDynamicTarget" : "ThinStaticWall",
                new Vector3(0f, 0f, 5f),
                new Vector3(4f, 4f, ThinTargetDepth),
                spawned);
            ProjectileDamageProbe targetDamage = target.AddComponent<ProjectileDamageProbe>();
            if (dynamicTarget)
            {
                Rigidbody targetBody = target.AddComponent<Rigidbody>();
                targetBody.useGravity = false;
                targetBody.isKinematic = false;
                targetBody.constraints = RigidbodyConstraints.FreezeAll;
                targetBody.collisionDetectionMode = CollisionDetectionMode.ContinuousSpeculative;
            }

            GameObject decoy = CreateBox(
                "FartherDecoy",
                new Vector3(0f, 0f, 8f),
                new Vector3(4f, 4f, ThinTargetDepth),
                spawned);
            ProjectileDamageProbe decoyDamage = decoy.AddComponent<ProjectileDamageProbe>();

            GameObject projectile = GameObject.CreatePrimitive(PrimitiveType.Sphere);
            projectile.name = "GeneratedFastProjectile";
            projectile.transform.position = new Vector3(0f, 0f, 0.65f);
            projectile.transform.localScale = Vector3.one * 0.08f;
            spawned.Add(projectile);
            Collider projectileCollider = projectile.GetComponent<Collider>();
            Rigidbody projectileBody = projectile.AddComponent<Rigidbody>();
            projectileBody.useGravity = false;
            projectileBody.isKinematic = false;
            ProjectileController controller = projectile.AddComponent<ProjectileController>();

            int poolReturns = 0;
            controller.SetPoolReturn(returnedProjectile =>
            {
                returnedProjectile.ResetForPool();
                returnedProjectile.gameObject.SetActive(false);
                poolReturns++;
            });

            Physics.SyncTransforms();
            controller.Configure(25f, owner, 2f, false);
            Assert.AreEqual(
                CollisionDetectionMode.ContinuousSpeculative,
                projectileBody.collisionDetectionMode,
                "Generated configuration must retain the supported CCD default.");
            Assert.IsTrue(
                Physics.GetIgnoreCollision(projectileCollider, ownerCollider),
                "Pool checkout must rebind owner collision exclusion.");

            // Isolate the generated Physics.SphereCast fallback. At 600 units/sec
            // this body crosses 12 units per fixed step, far beyond target depth.
            projectileBody.collisionDetectionMode = CollisionDetectionMode.Discrete;
            float startZ = projectile.transform.position.z;
            float furthestZ = startZ;
            int startFrame = Time.frameCount;
            controller.Launch(Vector3.forward * ProjectileSpeed);

            for (int step = 0; step < 8 && poolReturns == 0; step++)
            {
                yield return new WaitForFixedUpdate();
                furthestZ = Mathf.Max(furthestZ, projectile.transform.position.z);
            }
            yield return null;

            Assert.Greater(Time.frameCount, startFrame, "Play Mode frames did not advance.");
            Assert.Greater(furthestZ, startZ, "The projectile never moved in Play Mode.");
            Assert.AreEqual(1, targetDamage.HitCount, "First valid target must be hit once.");
            Assert.AreEqual(25f, targetDamage.TotalDamage, 0.001f);
            Assert.AreEqual(0, decoyDamage.HitCount, "A farther collider cannot win selection.");
            Assert.AreEqual(0, ownerDamage.HitCount, "The projectile must ignore its owner.");
            Assert.AreEqual(1, poolReturns, "Impact must return to the pool exactly once.");
            Assert.IsFalse(projectile.activeSelf, "Returned projectile must be parked.");
            Assert.IsFalse(
                Physics.GetIgnoreCollision(projectileCollider, ownerCollider),
                "Pool reset must restore the ignored collision pair.");

            Debug.Log(
                "SLICE2_TUNNEL_PASS target="
                + (dynamicTarget ? "dynamic" : "static")
                + " speed=" + ProjectileSpeed
                + " fixedStep=" + Time.fixedDeltaTime
                + " travelPerStep=" + (ProjectileSpeed * Time.fixedDeltaTime)
                + " targetDepth=" + ThinTargetDepth
                + " frames=" + (Time.frameCount - startFrame)
                + " startZ=" + startZ
                + " furthestZ=" + furthestZ
                + " hits=" + targetDamage.HitCount
                + " poolReturns=" + poolReturns);
        }
        finally
        {
            Time.fixedDeltaTime = originalFixedDeltaTime;
            foreach (GameObject instance in spawned)
            {
                if (instance != null)
                    Object.Destroy(instance);
            }
        }
    }

    private static GameObject CreateBox(
        string name,
        Vector3 position,
        Vector3 scale,
        ICollection<GameObject> spawned)
    {
        GameObject instance = GameObject.CreatePrimitive(PrimitiveType.Cube);
        instance.name = name;
        instance.transform.position = position;
        instance.transform.localScale = scale;
        spawned.Add(instance);
        return instance;
    }
}
