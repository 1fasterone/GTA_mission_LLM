using System.Collections;
using System.Collections.Generic;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.TestTools;

public sealed class ProjectileVisualReusePlayModeTests
{
    private static readonly Color BulletColor = new Color(1f, 0.58f, 0.08f, 1f);
    private static readonly Color RockColor = new Color(0.34f, 0.27f, 0.2f, 1f);

    [UnityTest]
    public IEnumerator BulletAndRockRemainDistinctThroughTwoFullReuseCycles()
    {
        var spawned = new List<GameObject>();
        Material bulletMaterial = null;
        Material rockMaterial = null;
        try
        {
            GameObject bulletPrefab = CreateProjectilePrefab(
                "Projectile3DPrefab_FastRound",
                "ProjectileFallback_FastRound_Capsule",
                PrimitiveType.Capsule,
                0.08f,
                new Vector3(90f, 0f, 0f),
                BulletColor,
                out bulletMaterial);
            spawned.Add(bulletPrefab);
            GameObject rockPrefab = CreateProjectilePrefab(
                "Projectile3DPrefab_ThrownObject",
                "ProjectileFallback_ThrownObject_Cube",
                PrimitiveType.Cube,
                0.22f,
                new Vector3(18f, 31f, 12f),
                RockColor,
                out rockMaterial);
            spawned.Add(rockPrefab);

            ProjectilePool bulletPool = CreatePool("BulletPool", bulletPrefab, spawned);
            ProjectilePool rockPool = CreatePool("RockPool", rockPrefab, spawned);

            int startFrame = Time.frameCount;
            for (int use = 0; use < 3; use++)
            {
                ProjectileController bullet = bulletPool.Borrow();
                ProjectileController rock = rockPool.Borrow();
                Assert.IsNotNull(bullet, "The fixed bullet pool lost its reusable instance.");
                Assert.IsNotNull(rock, "The fixed rock pool lost its reusable instance.");
                yield return null;

                AssertPresentation(
                    bullet,
                    "ProjectileFallback_FastRound_Capsule",
                    0.08f,
                    BulletColor);
                AssertPresentation(
                    rock,
                    "ProjectileFallback_ThrownObject_Cube",
                    0.22f,
                    RockColor);
                Assert.AreNotEqual(
                    FindVisual(bullet).name,
                    FindVisual(rock).name,
                    "Closed profiles must not collapse to one visual identity.");

                MutateTransientVisualState(bullet, use);
                MutateTransientVisualState(rock, use);
                bulletPool.Return(bullet);
                rockPool.Return(rock);
                yield return null;

                AssertParked(bullet, bulletPool.transform);
                AssertParked(rock, rockPool.transform);
            }

            Assert.Greater(Time.frameCount, startFrame, "Play Mode frames did not advance.");
            Debug.Log(
                "SLICE3_VISUAL_REUSE_PASS uses=3 reuseCycles=2"
                + " bullet=ProjectileFallback_FastRound_Capsule"
                + " rock=ProjectileFallback_ThrownObject_Cube"
                + " frameAdvance=" + (Time.frameCount - startFrame));
        }
        finally
        {
            foreach (GameObject instance in spawned)
            {
                if (instance != null)
                    Object.Destroy(instance);
            }
            if (bulletMaterial != null) Object.Destroy(bulletMaterial);
            if (rockMaterial != null) Object.Destroy(rockMaterial);
        }
    }

    private static ProjectilePool CreatePool(
        string name,
        GameObject prefab,
        ICollection<GameObject> spawned)
    {
        var poolObject = new GameObject(name);
        spawned.Add(poolObject);
        ProjectilePool pool = poolObject.AddComponent<ProjectilePool>();
        pool.Initialize(prefab, 1, ProjectilePoolGrowthPolicy.Fixed);
        return pool;
    }

    private static GameObject CreateProjectilePrefab(
        string rootName,
        string visualName,
        PrimitiveType primitive,
        float uniformScale,
        Vector3 localEuler,
        Color color,
        out Material material)
    {
        var prefab = new GameObject(rootName);
        prefab.SetActive(false);
        var physicsShape = prefab.AddComponent<SphereCollider>();
        physicsShape.radius = Mathf.Max(0.01f, uniformScale * 0.5f);
        var body = prefab.AddComponent<Rigidbody>();
        body.isKinematic = true;
        body.useGravity = false;
        body.collisionDetectionMode = CollisionDetectionMode.ContinuousSpeculative;
        prefab.AddComponent<ProjectileController>();

        var visualRoot = new GameObject("ProjectileVisualRoot");
        visualRoot.transform.SetParent(prefab.transform, false);
        visualRoot.transform.localRotation = Quaternion.Euler(localEuler);
        visualRoot.transform.localScale = Vector3.one * uniformScale;

        GameObject visual = GameObject.CreatePrimitive(primitive);
        visual.name = visualName;
        visual.transform.SetParent(visualRoot.transform, false);
        visual.transform.SetLocalPositionAndRotation(Vector3.zero, Quaternion.identity);
        visual.transform.localScale = Vector3.one;

        Shader shader = Shader.Find("Sprites/Default");
        if (shader == null) shader = Shader.Find("Standard");
        if (shader == null) shader = Shader.Find("Unlit/Color");
        Assert.IsNotNull(shader, "Acceptance project has no supported color shader.");
        material = new Material(shader);
        material.color = color;
        visual.GetComponent<Renderer>().sharedMaterial = material;

        TrailRenderer trail = visual.AddComponent<TrailRenderer>();
        trail.time = 2f;
        trail.emitting = true;
        trail.sharedMaterial = material;

        ParticleSystem particles = visual.AddComponent<ParticleSystem>();
        ParticleSystem.MainModule main = particles.main;
        main.playOnAwake = false;
        particles.useAutoRandomSeed = false;
        particles.randomSeed = 7331u;
        return prefab;
    }

    private static void AssertPresentation(
        ProjectileController projectile,
        string visualName,
        float expectedScale,
        Color expectedColor)
    {
        Transform visualRoot = projectile.transform.Find("ProjectileVisualRoot");
        Assert.IsNotNull(visualRoot, "Visual root leaked or was removed.");
        Assert.That(visualRoot.localPosition, Is.EqualTo(Vector3.zero));
        Assert.AreEqual(expectedScale, visualRoot.localScale.x, 0.0001f);
        Assert.AreEqual(visualRoot.localScale.x, visualRoot.localScale.y, 0.0001f);
        Assert.AreEqual(visualRoot.localScale.x, visualRoot.localScale.z, 0.0001f);

        Transform visual = visualRoot.Find(visualName);
        Assert.IsNotNull(visual, "Named deterministic fallback is missing.");
        Assert.That(visual.localPosition, Is.EqualTo(Vector3.zero));
        Assert.That(visual.localRotation, Is.EqualTo(Quaternion.identity));
        Assert.That(visual.localScale, Is.EqualTo(Vector3.one));

        Renderer renderer = visual.GetComponent<Renderer>();
        Assert.IsTrue(renderer.enabled, "Renderer enabled state leaked between uses.");
        AssertColor(expectedColor, renderer.sharedMaterial.color);
        var propertyBlock = new MaterialPropertyBlock();
        renderer.GetPropertyBlock(propertyBlock);
        Assert.IsTrue(propertyBlock.isEmpty, "Material property block leaked between uses.");

        ParticleSystem particles = visual.GetComponent<ParticleSystem>();
        Assert.AreEqual(0, particles.particleCount, "Particles leaked between uses.");
        Assert.AreEqual(7331u, particles.randomSeed, "Particle seed leaked between uses.");
        Assert.IsFalse(particles.useAutoRandomSeed);

        TrailRenderer trail = visual.GetComponent<TrailRenderer>();
        Assert.AreEqual(0, trail.positionCount, "Trail geometry leaked between uses.");
        Assert.IsTrue(trail.enabled, "Trail enabled state leaked between uses.");
        Assert.IsTrue(trail.emitting, "Trail emitting state was not restored on checkout.");

        Collider[] rootShapes = projectile.GetComponents<Collider>();
        Assert.AreEqual(1, rootShapes.Length, "Physics root must own one primitive shape.");
        Assert.IsTrue(rootShapes[0] is SphereCollider);
        Assert.IsTrue(rootShapes[0].enabled);
        foreach (Collider visualShape in visualRoot.GetComponentsInChildren<Collider>(true))
            Assert.IsFalse(visualShape.enabled, "Visual mesh must not define collision.");
        foreach (Rigidbody visualBody in visualRoot.GetComponentsInChildren<Rigidbody>(true))
            Assert.IsFalse(visualBody.detectCollisions, "Visual child body retained physics authority.");
    }

    private static Transform FindVisual(ProjectileController projectile)
    {
        Transform root = projectile.transform.Find("ProjectileVisualRoot");
        Assert.IsNotNull(root);
        Assert.Greater(root.childCount, 0);
        return root.GetChild(0);
    }

    private static void MutateTransientVisualState(ProjectileController projectile, int use)
    {
        Transform visualRoot = projectile.transform.Find("ProjectileVisualRoot");
        Transform visual = FindVisual(projectile);
        projectile.transform.rotation = Quaternion.Euler(27f + use, 41f, 13f);
        projectile.transform.localScale = new Vector3(2f, 3f, 4f);
        visualRoot.localPosition = new Vector3(5f, 6f, 7f);
        visualRoot.localRotation = Quaternion.Euler(66f, 77f, 88f);
        visualRoot.localScale = new Vector3(3f, 2f, 1f);
        visual.SetParent(projectile.transform, false);
        visual.localPosition = new Vector3(-2f, 4f, 9f);
        visual.localRotation = Quaternion.Euler(12f, 34f, 56f);
        visual.localScale = new Vector3(8f, 7f, 6f);
        visual.gameObject.SetActive(false);

        Renderer renderer = visual.GetComponent<Renderer>();
        renderer.enabled = false;
        renderer.material.color = Color.magenta;
        var propertyBlock = new MaterialPropertyBlock();
        propertyBlock.SetColor("_Color", Color.green);
        renderer.SetPropertyBlock(propertyBlock);

        TrailRenderer trail = visual.GetComponent<TrailRenderer>();
        trail.enabled = false;
        trail.emitting = false;
        trail.AddPosition(Vector3.zero);
        trail.AddPosition(Vector3.one);

        ParticleSystem particles = visual.GetComponent<ParticleSystem>();
        particles.randomSeed = (uint)(9000 + use);
        particles.Emit(5);

        var transient = new GameObject("TransientLeak_" + use);
        transient.transform.SetParent(projectile.transform, false);
    }

    private static void AssertParked(ProjectileController projectile, Transform poolTransform)
    {
        Assert.IsFalse(projectile.gameObject.activeSelf, "Returned instance is still active.");
        Assert.AreSame(poolTransform, projectile.transform.parent);
        Assert.That(projectile.transform.localPosition, Is.EqualTo(Vector3.zero));
        Assert.That(projectile.transform.localRotation, Is.EqualTo(Quaternion.identity));
        Assert.That(projectile.transform.localScale, Is.EqualTo(Vector3.one));
        Assert.IsNull(
            projectile.transform.Find("TransientLeak_0"),
            "Transient visual child leaked through return.");
        Assert.IsNull(projectile.transform.Find("TransientLeak_1"));
        Assert.IsNull(projectile.transform.Find("TransientLeak_2"));
    }

    private static void AssertColor(Color expected, Color actual)
    {
        Assert.AreEqual(expected.r, actual.r, 0.001f);
        Assert.AreEqual(expected.g, actual.g, 0.001f);
        Assert.AreEqual(expected.b, actual.b, 0.001f);
        Assert.AreEqual(expected.a, actual.a, 0.001f);
    }
}
