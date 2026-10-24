using System.Collections;
using System.Reflection;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.TestTools;

public sealed class ProjectileSlice3VisualPlayModeTests
{
    [UnityTest]
    public IEnumerator BulletAndRockFallbacksStayDistinctAcrossTwoReuseCycles()
    {
        var controllerHost = new GameObject("Slice3ControllerHost");
        controllerHost.SetActive(false);
        var controller = controllerHost.AddComponent<ProjectileSlice3Acceptance>();
        var prefabOwner = new GameObject("Slice3PrefabOwner");
        MethodInfo build = typeof(ProjectileSlice3Acceptance).GetMethod(
            "BuildProjectile3DPrefab",
            BindingFlags.Instance | BindingFlags.NonPublic);
        Assert.That(build, Is.Not.Null);

        var bulletPrefab = (GameObject)build.Invoke(
            controller, new object[] { 0, prefabOwner.transform });
        var rockPrefab = (GameObject)build.Invoke(
            controller, new object[] { 1, prefabOwner.transform });

        AssertFallbackContract(
            bulletPrefab, "ProjectileVisualFallback_Bullet", "Capsule", 0.04f);
        AssertFallbackContract(
            rockPrefab, "ProjectileVisualFallback_Rock", "Sphere", 0.11f);
        Color bulletColor = FallbackRenderer(bulletPrefab).sharedMaterial.color;
        Color rockColor = FallbackRenderer(rockPrefab).sharedMaterial.color;
        Assert.That(bulletColor, Is.Not.EqualTo(rockColor));

        yield return VerifyTwoReuseCycles(bulletPrefab, "bullet");
        yield return VerifyTwoReuseCycles(rockPrefab, "rock");

        Debug.Log(
            "SLICE3_VISUAL_PASS profiles=bullet,rock reuse_cycles=2 "
            + "physics=root-sphere presentation=distinct-primitives");
        Object.Destroy(controllerHost);
        Object.Destroy(prefabOwner);
    }

    private static void AssertFallbackContract(
        GameObject prefab,
        string visualName,
        string meshName,
        float expectedRadius)
    {
        Assert.That(prefab, Is.Not.Null);
        Assert.That(prefab.transform.localScale, Is.EqualTo(Vector3.one));
        var rootCollider = prefab.GetComponent<SphereCollider>();
        Assert.That(rootCollider, Is.Not.Null);
        Assert.That(rootCollider.radius, Is.EqualTo(expectedRadius).Within(0.0001f));
        Assert.That(prefab.GetComponent<Rigidbody>(), Is.Not.Null);

        var filter = prefab.GetComponentInChildren<MeshFilter>(true);
        Assert.That(filter, Is.Not.Null);
        Transform visual = filter.transform;
        Assert.That(visual.name, Is.EqualTo(visualName));
        Assert.That(filter.sharedMesh.name, Does.Contain(meshName));
        Assert.That(FallbackRenderer(prefab).enabled, Is.True);
        foreach (Collider collider in visual.GetComponentsInChildren<Collider>(true))
            Assert.That(collider.enabled, Is.False);
    }

    private static IEnumerator VerifyTwoReuseCycles(GameObject prefab, string profile)
    {
        Transform prefabVisual = prefab.transform.GetChild(0);
        var particle = prefabVisual.gameObject.AddComponent<ParticleSystem>();
        var main = particle.main;
        main.playOnAwake = false;

        var poolObject = new GameObject("Slice3Pool_" + profile);
        var pool = poolObject.AddComponent<ProjectilePool>();
        pool.Initialize(prefab, 1, ProjectilePoolGrowthPolicy.Fixed);

        ProjectileController prior = null;
        for (int cycle = 1; cycle <= 2; cycle++)
        {
            ProjectileController projectile = pool.Borrow();
            Assert.That(projectile, Is.Not.Null);
            if (prior != null) Assert.That(projectile, Is.SameAs(prior));
            prior = projectile;

            Transform visual = projectile.transform.GetChild(0);
            Vector3 expectedPosition = visual.localPosition;
            Quaternion expectedRotation = visual.localRotation;
            Vector3 expectedScale = visual.localScale;
            Renderer renderer = FallbackRenderer(projectile.gameObject);
            Material expectedMaterial = renderer.sharedMaterial;
            TrailRenderer trail = projectile.GetComponentInChildren<TrailRenderer>(true);
            ParticleSystem effect = projectile.GetComponentInChildren<ParticleSystem>(true);

            Assert.That(projectile.transform.localScale, Is.EqualTo(Vector3.one));
            Assert.That(renderer.enabled, Is.True);
            Assert.That(effect, Is.Not.Null);
            Assert.That(effect.isPlaying, Is.False);
            if (trail != null) Assert.That(trail.emitting, Is.False);

            projectile.transform.SetPositionAndRotation(
                new Vector3(0f, 1f, cycle * 2f), Quaternion.Euler(5f, 20f, 0f));
            projectile.Configure(10f, poolObject, 2f, profile == "rock");
            projectile.Launch(Vector3.forward * 2f);
            yield return new WaitForFixedUpdate();
            Assert.That(effect.isPlaying, Is.True);
            if (trail != null) Assert.That(trail.emitting, Is.True);

            visual.localPosition += new Vector3(1f, 2f, 3f);
            visual.localRotation = Quaternion.Euler(70f, 80f, 90f);
            visual.localScale = Vector3.one * 3f;
            renderer.sharedMaterial = new Material(expectedMaterial.shader)
            {
                name = "MutatedMaterial"
            };
            pool.Return(projectile);

            Assert.That(projectile.transform.parent, Is.SameAs(poolObject.transform));
            Assert.That(projectile.transform.localPosition, Is.EqualTo(Vector3.zero));
            Assert.That(projectile.transform.localRotation, Is.EqualTo(Quaternion.identity));
            Assert.That(projectile.transform.localScale, Is.EqualTo(Vector3.one));
            Assert.That(visual.localPosition, Is.EqualTo(expectedPosition));
            Assert.That(visual.localRotation, Is.EqualTo(expectedRotation));
            Assert.That(visual.localScale, Is.EqualTo(expectedScale));
            Assert.That(renderer.sharedMaterial, Is.SameAs(expectedMaterial));
            Assert.That(effect.isPlaying, Is.False);
            if (trail != null) Assert.That(trail.emitting, Is.False);

            Debug.Log(
                "SLICE3_REUSE_PASS profile=" + profile + " cycle=" + cycle
                + " transforms=reset materials=reset effects=reset");
        }
        Object.Destroy(poolObject);
    }

    private static Renderer FallbackRenderer(GameObject root)
    {
        foreach (Renderer renderer in root.GetComponentsInChildren<Renderer>(true))
        {
            if (!(renderer is TrailRenderer)) return renderer;
        }
        Assert.Fail("No visible fallback renderer found under " + root.name);
        return null;
    }
}
