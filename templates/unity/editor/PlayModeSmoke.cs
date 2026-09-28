// Generic, genre-agnostic PlayMode smoke test.
//
// Dropped into <project>/Assets/PlayModeSmoke/ by `python unity_cli.py
// playtest` (also reachable via `e2e-verify --playtest`), together with
// PlayModeSmoke.Tests.asmdef, and cleaned up afterward. NOT part of normal
// generated output.
//
// Run via:
//   Unity.exe -batchmode -nographics -projectPath <p> -runTests
//             -testPlatform PlayMode -testResults <xml> -logFile <log>
//
// Every check reads its expectations from playtest_expectations.json,
// written by src/unity_playtest.py (Python side) next to compile_report.json
// in the project root — never inside Assets/, so it is never imported as an
// asset. This assembly deliberately has NO reference to Assembly-CSharp (see
// PlayModeSmoke.Tests.asmdef): every check works through Unity's own tag
// system, GameObject-name conventions EntitySpawner.cs.j2 already commits to
// (`"{groupName}_{i}"`), or reflection — never a generated project's own
// class names, which vary per prompt.
//
// Each [UnityTest] loads the game's real main scene itself
// (SceneManager.LoadScene(0, ...) — RUNTIME loader, by build index, never
// EditorSceneManager) so every check gets a fresh Awake/Start pass exactly
// like a real player's build, sidestepping EntitySpawner's
// [ExecuteAlways]/_baked edit-mode guard entirely.

using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using NUnit.Framework;
using UnityEditor;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;

public class PlayModeSmoke
{
    // Do not touch LogAssert during editor-domain load. Unity 6 requires an
    // active log scope for that property; changing it from an
    // [InitializeOnLoadMethod] throws before the PlayMode fixture can run.
    // NoConsoleErrorsDuringPlay owns the generated project's log contract via
    // its independent Application.logMessageReceived capture below.

    [Serializable]
    private class EntityExpectationDto
    {
        public string name;
        public int    expected_count;
        public bool   exact = true;
    }

    [Serializable]
    private class ExpectationsDto
    {
        public string locomotion = "walk";
        public bool   check_player_tag = true;
        public EntityExpectationDto[] entities;
    }

    private static readonly string ProjectRoot =
        Directory.GetParent(Application.dataPath).FullName;
    private static readonly string ExpectationsPath =
        Path.Combine(ProjectRoot, "playtest_expectations.json");

    // Read once, eagerly: [ValueSource] runs at NUnit test-discovery time,
    // which can happen before any [OneTimeSetUp].
    private static readonly ExpectationsDto _expectations = LoadExpectations();

    private static ExpectationsDto LoadExpectations()
    {
        try
        {
            if (!File.Exists(ExpectationsPath)) return null;
            return JsonUtility.FromJson<ExpectationsDto>(File.ReadAllText(ExpectationsPath));
        }
        catch (Exception)
        {
            return null;
        }
    }

    // A missing/corrupt sidecar must surface as one loud FAILING case, never
    // as zero silently-skipped EntityGroupSpawned cases (an empty [entities]
    // array is a legitimate project with no groups — that yields zero cases
    // on purpose and is NOT this sentinel).
    private const string MissingExpectationsSentinel = "__missing_expectations__";

    private static IEnumerable<string> EntityGroupNames()
    {
        if (_expectations == null || _expectations.entities == null)
        {
            yield return MissingExpectationsSentinel;
            yield break;
        }
        foreach (var e in _expectations.entities)
            yield return e.name;
    }

    // EntitySpawner.Start() has no coroutine (confirmed: no IEnumerator /
    // StartCoroutine / yield anywhere in EntitySpawner.cs.j2) — spawning
    // completes synchronously within the frame the scene finishes loading.
    // A handful of settle frames covers the load itself, not the spawn.
    private const int SettleFrames = 8;

    private static IEnumerator LoadMainSceneAndSettle()
    {
        SceneManager.LoadScene(0, LoadSceneMode.Single);
        for (int i = 0; i < SettleFrames; i++)
            yield return null;
    }

    private static List<Transform> AllTransformsInActiveScene()
    {
        var result = new List<Transform>();
        foreach (var root in SceneManager.GetActiveScene().GetRootGameObjects())
            result.AddRange(root.GetComponentsInChildren<Transform>(true));  // include inactive
        return result;
    }

    private readonly List<string> _capturedLogs = new List<string>();

    private void OnLog(string logString, string stackTrace, LogType type)
    {
        if (type == LogType.Error || type == LogType.Exception || type == LogType.Assert)
            _capturedLogs.Add(type + ": " + logString);
    }

    [UnityTest]
    public IEnumerator NoConsoleErrorsDuringPlay()
    {
        _capturedLogs.Clear();
        Application.logMessageReceived += OnLog;
        yield return LoadMainSceneAndSettle();
        Application.logMessageReceived -= OnLog;

        if (_capturedLogs.Count > 0)
            Assert.Fail(_capturedLogs.Count + " error(s)/exception(s) logged during play:\n" +
                        string.Join("\n", _capturedLogs.Take(10)));
    }

    [UnityTest]
    public IEnumerator PlayerTagCardinality()
    {
        if (_expectations == null)
        {
            Assert.Fail("playtest_expectations.json missing or corrupt");
            yield break;
        }
        if (!_expectations.check_player_tag)
        {
            Assert.Ignore("locomotion='" + _expectations.locomotion + "' — no avatar expected");
            yield break;
        }
        yield return LoadMainSceneAndSettle();

        var tagged = GameObject.FindGameObjectsWithTag("Player");  // active objects only, by definition
        Assert.AreEqual(1, tagged.Length,
            "expected exactly 1 active Player-tagged GameObject, found " + tagged.Length +
            (tagged.Length > 0 ? ": " + string.Join(", ", tagged.Select(g => g.name)) : ""));
    }

    private static bool TryGetClimbingRoute(out List<Transform> route)
    {
        route = new List<Transform>();
        try
        {
            foreach (string tag in new[] { "Hold", "Platform" })
                foreach (var go in GameObject.FindGameObjectsWithTag(tag))
                    if (go != null && !route.Contains(go.transform)) route.Add(go.transform);
        }
        catch (UnityException) { }
        route.Sort((a, b) => a.position.y == b.position.y
            ? a.position.x.CompareTo(b.position.x)
            : a.position.y.CompareTo(b.position.y));
        return route.Count >= 2;
    }

    private static Bounds PlayerBounds(GameObject player)
    {
        var renderers = player.GetComponentsInChildren<Renderer>(true);
        if (renderers.Length > 0)
        {
            Bounds bounds = renderers[0].bounds;
            for (int i = 1; i < renderers.Length; i++) bounds.Encapsulate(renderers[i].bounds);
            return bounds;
        }
        var collider = player.GetComponent<Collider>();
        return collider != null ? collider.bounds : new Bounds(player.transform.position, Vector3.one);
    }

    [UnityTest]
    public IEnumerator ClimbingCameraPlayerVisibility()
    {
        yield return LoadMainSceneAndSettle();
        List<Transform> route;
        if (!TryGetClimbingRoute(out route))
        {
            Assert.Ignore("no Hold/Platform route — climbing contract not present");
            yield break;
        }

        var player = GameObject.FindWithTag("Player");
        Assert.IsNotNull(player, "climbing route requires a Player-tagged avatar");
        var camera = Camera.main;
        Assert.IsNotNull(camera, "climbing camera contract requires Camera.main");
        Bounds bounds = PlayerBounds(player);
        var planes = GeometryUtility.CalculateFrustumPlanes(camera);
        Assert.IsTrue(GeometryUtility.TestPlanesAABB(planes, bounds),
            "player bounds are outside Camera.main; follow framing is not playable " +
            "player=" + bounds.center + " camera=" + camera.transform.position +
            " forward=" + camera.transform.forward);
        Vector3 viewport = camera.WorldToViewportPoint(bounds.center);
        Assert.IsTrue(viewport.z > 0f && viewport.x > 0.02f && viewport.x < 0.98f
                      && viewport.y > 0.02f && viewport.y < 0.98f,
            "player bounds are not actually visible through Camera.main " +
            "viewport=" + viewport);
    }

    [UnityTest]
    public IEnumerator ClimbingRouteIsReachable()
    {
        yield return LoadMainSceneAndSettle();
        List<Transform> route;
        if (!TryGetClimbingRoute(out route))
        {
            Assert.Ignore("no Hold/Platform route — climbing contract not present");
            yield break;
        }
        for (int i = 1; i < route.Count; i++)
            Assert.LessOrEqual(Vector3.Distance(route[i - 1].position, route[i].position), 4.0f,
                "successive climbing holds exceed the deterministic reach budget");

        var player = GameObject.FindWithTag("Player");
        Assert.IsNotNull(player, "climbing route requires a Player-tagged avatar");
        Component controller = player.GetComponents<MonoBehaviour>()
            .FirstOrDefault(c => c != null && c.GetType().GetProperty("HoldCount") != null);
        Assert.IsNotNull(controller, "generated player controller has no climbing route contract");
        int holdCount = (int)controller.GetType().GetProperty("HoldCount").GetValue(controller, null);
        Assert.GreaterOrEqual(holdCount, 2, "controller did not bind the visible hold route");
    }

    [UnityTest]
    public IEnumerator ClimbingFlagScaleIsBounded()
    {
        yield return LoadMainSceneAndSettle();
        List<Transform> route;
        if (!TryGetClimbingRoute(out route))
        {
            Assert.Ignore("no Hold/Platform route — climbing contract not present");
            yield break;
        }
        GameObject flag = null;
        try { flag = GameObject.FindWithTag("Checkpoint"); }
        catch (UnityException) { }
        if (flag == null)
            flag = AllTransformsInActiveScene().Select(t => t.gameObject)
                .FirstOrDefault(g => g.name.StartsWith("SummitFlag_", StringComparison.Ordinal));
        Assert.IsNotNull(flag, "climbing route has no summit flag/checkpoint");
        float maxScale = Mathf.Max(flag.transform.lossyScale.x,
                                   Mathf.Max(flag.transform.lossyScale.y, flag.transform.lossyScale.z));
        Assert.LessOrEqual(maxScale, 0.70f,
            "summit flag is not using the deterministic small-landmark scale");
    }

    [UnityTest]
    public IEnumerator ClimbingAnimationStateIsActive()
    {
        yield return LoadMainSceneAndSettle();
        List<Transform> route;
        if (!TryGetClimbingRoute(out route))
        {
            Assert.Ignore("no Hold/Platform route — climbing contract not present");
            yield break;
        }
        var player = GameObject.FindWithTag("Player");
        var animator = player == null ? null : player.GetComponentInChildren<Animator>(true);
        Assert.IsNotNull(animator, "climbing avatar has no Animator");
        Assert.IsNotNull(animator.runtimeAnimatorController,
            "climbing avatar has no runtime AnimatorController");
        Assert.IsTrue(animator.parameters.Any(p => p.name == "Climb"),
            "generated AnimatorController has no Climb parameter");
        Assert.IsTrue(animator.HasState(0, Animator.StringToHash("Climb")),
            "generated AnimatorController has no Climb state");
    }

    [UnityTest]
    public IEnumerator EntityGroupSpawned([ValueSource(nameof(EntityGroupNames))] string groupName)
    {
        if (groupName == MissingExpectationsSentinel)
        {
            Assert.Fail("playtest_expectations.json missing or corrupt — no entity expectations loaded");
            yield break;
        }
        var exp = _expectations.entities.First(e => e.name == groupName);
        yield return LoadMainSceneAndSettle();

        string prefix = groupName + "_";
        int actual = AllTransformsInActiveScene()
            .Count(t => t.name.StartsWith(prefix, StringComparison.Ordinal));

        if (exp.exact)
            Assert.AreEqual(exp.expected_count, actual,
                groupName + ": expected exactly " + exp.expected_count + ", found " + actual);
        else
            Assert.GreaterOrEqual(actual, exp.expected_count,
                groupName + ": expected at least " + exp.expected_count + ", found " + actual);
    }

    [UnityTest]
    public IEnumerator UiTextContentPresent()
    {
        yield return LoadMainSceneAndSettle();

        // Reflection-only — never `using TMPro;` / `using UnityEngine.UI;` —
        // so a project missing either optional package can't take down this
        // (or any other) check in this fixture.
        Type tmpType  = Type.GetType("TMPro.TMP_Text, Unity.TextMeshPro");
        Type uguiType = Type.GetType("UnityEngine.UI.Text, UnityEngine.UI");

        bool found = false;
        foreach (var t in new[] { tmpType, uguiType })
        {
            if (t == null) continue;
            var prop = t.GetProperty("text");
            if (prop == null) continue;
            foreach (var root in SceneManager.GetActiveScene().GetRootGameObjects())
            {
                foreach (Component c in root.GetComponentsInChildren(t, true))
                {
                    var val = prop.GetValue(c) as string;
                    if (!string.IsNullOrWhiteSpace(val)) { found = true; break; }
                }
                if (found) break;
            }
            if (found) break;
        }

        // Informational only — never gates the build. Not every valid game
        // has HUD text (see UnitySchema.locomotion="none" board/UI games,
        // and plenty of embodied games with a diegetic-only HUD).
        Assert.Pass(found ? "found populated text content" : "no populated text component found (informational only)");
    }

    // Gap C's other half (docs/COMPILER_AUDIT.md solution #4): "does it
    // boot" is covered above; this is "does it look right". Mechanism only —
    // capture a real frame and save it. The actual perceptual judgment (solid
    // black? Unity's magenta missing-shader color dominant?) happens in
    // src/unity_playtest.py's analyze_render_health(), the same "Unity owns
    // mechanism, Python owns judgment" split Step 7 already uses. Needs a
    // real graphics device — the caller must NOT pass -nographics for this
    // run (unlike the warm-up pass, which never renders a frame).
    //
    // Three dead ends here, all confirmed live (2026-09), worth recording so
    // the next person doesn't retry them:
    //   1. `yield return new WaitForEndOfFrame();` + ScreenCapture — batchmode
    //      logs "UnityTest yielded WaitForEndOfFrame, which is not evoked in
    //      batchmode" (no real frame-presentation loop without a window).
    //   2. A manually-triggered `camera.Render()` into an owned RenderTexture
    //      — synchronous, no frame-timing dependency, sounds right — but on
    //      a URP project it rendered 100% Unity's magenta missing-shader
    //      color plus a "QuaternionToEuler: Input quaternion was not
    //      normalized" assert that never fires for the SAME scene in every
    //      other check in this fixture. A second, manually-invoked render
    //      evidently doesn't route through URP's own scriptable render
    //      pipeline the way the camera's normal per-frame render does.
    //   3. `ScreenCapture.CaptureScreenshotIntoRenderTexture` after a plain
    //      `yield return null` — no error this time, but the captured frame
    //      came back 100% solid black: batchmode's screen/window backbuffer
    //      evidently never gets a real frame composited into it even without
    //      `-nographics`, so there's nothing there to grab.
    // The fix: assign the RenderTexture as the camera's OWN targetTexture
    // BEFORE the next `yield return null`, so Unity's normal, already-proven
    // per-frame render loop (the same one all 9 other checks in this fixture
    // already render through without issue) draws into it directly — never
    // touching the window backbuffer (which #3 showed is empty) and never
    // manually re-invoking Render() outside that normal loop (which #2 showed
    // URP doesn't handle the same way).
    private sealed class VisualComparisonCaptureState
    {
        private readonly List<GameObject> _disabledObjects = new List<GameObject>();
        private readonly List<Behaviour> _disabledBehaviours = new List<Behaviour>();
        private readonly bool _fogEnabled;
        private bool _restored;

        private VisualComparisonCaptureState()
        {
            _fogEnabled = RenderSettings.fog;
        }

        public static VisualComparisonCaptureState Begin()
        {
            var state = new VisualComparisonCaptureState();
            RenderSettings.fog = false;

            foreach (var root in SceneManager.GetActiveScene().GetRootGameObjects())
            {
                foreach (var transform in root.GetComponentsInChildren<Transform>(true))
                {
                    var gameObject = transform.gameObject;
                    if (IsUiOrAtmosphereObject(gameObject.name))
                    {
                        if (gameObject.activeSelf)
                        {
                            gameObject.SetActive(false);
                            state._disabledObjects.Add(gameObject);
                        }
                        continue;
                    }

                    foreach (var behaviour in gameObject.GetComponents<Behaviour>())
                    {
                        if (IsUiOrAtmosphereBehaviour(behaviour) && behaviour.enabled)
                        {
                            behaviour.enabled = false;
                            state._disabledBehaviours.Add(behaviour);
                        }
                    }
                }
            }
            return state;
        }

        public void Restore()
        {
            if (_restored) return;
            for (int i = _disabledBehaviours.Count - 1; i >= 0; i--)
            {
                if (_disabledBehaviours[i] != null)
                    _disabledBehaviours[i].enabled = true;
            }
            for (int i = _disabledObjects.Count - 1; i >= 0; i--)
            {
                if (_disabledObjects[i] != null)
                    _disabledObjects[i].SetActive(true);
            }
            RenderSettings.fog = _fogEnabled;
            _restored = true;
        }

        private static bool IsUiOrAtmosphereObject(string name)
        {
            string token = name.ToLowerInvariant().Replace(" ", "").Replace("_", "");
            return token == "canvas" || token == "hudroot" || token == "eventsystem" ||
                   token == "volume" || token.Contains("pausemenu") ||
                   token.Contains("gameover") || token.Contains("atmosphere") ||
                   token.Contains("postprocess");
        }

        private static bool IsUiOrAtmosphereBehaviour(Behaviour behaviour)
        {
            string token = behaviour.GetType().Name.ToLowerInvariant();
            return token == "canvas" || token == "uidocument" ||
                   token == "volume" || token.EndsWith("volume", StringComparison.Ordinal) ||
                   token.Contains("atmosphere") || token.Contains("postprocess");
        }
    }

    public const string ScreenshotFilename = "playtest_screenshot.png";

    [UnityTest]
    public IEnumerator CaptureMainCameraScreenshot()
    {
        yield return LoadMainSceneAndSettle();

        Camera cam = Camera.main;
        if (cam == null)
        {
            // Camera.main requires the "MainCamera" tag — a generated scene
            // isn't guaranteed to use it. Any active camera is good enough
            // for a health check.
            foreach (var c in UnityEngine.Object.FindObjectsByType<Camera>(FindObjectsSortMode.None))
            {
                if (c.isActiveAndEnabled) { cam = c; break; }
            }
        }
        Assert.IsNotNull(cam, "no active Camera found in the loaded scene to capture from");

        // Five dead ends/findings here, all confirmed live (2026-09), worth
        // recording so the next person doesn't retry them:
        //   1-3. Reassigning the SCENE's own camera's targetTexture (three
        //        different ways) consistently triggered a native (no C#
        //        stack trace at all) "QuaternionToEuler: Input quaternion was
        //        not normalized" the instant it happened — this project has
        //        Cinemachine actively driving that camera.
        //   4. Instantiate(cam.gameObject) — a full clone, so URP's
        //      UniversalAdditionalCameraData comes along faithfully — then
        //      disabling (not just Destroy()ing, which is deferred to
        //      end-of-frame) anything Cinemachine-named on the clone. The
        //      assert came right back anyway: Instantiate() runs Awake()/
        //      OnEnable() on every cloned component SYNCHRONOUSLY, before any
        //      of my own code gets a chance to disable one.
        //   5. A genuinely fresh GameObject (never cloned from anything
        //      Cinemachine touched) + CopyFrom(cam) cleared the assert, but
        //      every capture still rendered 100% Unity's magenta
        //      missing-shader color — not a capture-mechanism bug at all:
        //      GraphicsSettings.defaultRenderPipeline was null (built-in RP
        //      active, not URP), a genuine, previously-invisible bug in
        //      SetupRenderPipeline.cs.j2's first-open setup gate — fixed
        //      there (see that file's own comment). Nothing left to work
        //      around here once that's fixed at the source.
        int width = 640;
        int height = 360;
        var rt = new RenderTexture(width, height, 24);
        var tex = new Texture2D(width, height, TextureFormat.RGB24, false);
        var captureGo = new GameObject("PlayModeSmokeCaptureCamera");
        var prevActive = RenderTexture.active;
        VisualComparisonCaptureState captureState = null;
        try
        {
            captureState = VisualComparisonCaptureState.Begin();
            var captureCam = captureGo.AddComponent<Camera>();
            captureCam.CopyFrom(cam);
            captureGo.transform.SetPositionAndRotation(cam.transform.position, cam.transform.rotation);
            // The UI layer is a final safety net for canvases that were
            // created without a recognizable root name.
            captureCam.cullingMask &= ~(1 << 5);

            // URP needs UniversalAdditionalCameraData on a camera for
            // correct per-camera rendering; CopyFrom() only copies base
            // Camera fields. Reflection-only — never a hard reference to
            // URP — matching UiTextContentPresent below not
            // hard-referencing TMPro/UGUI.
            Type urpDataType = Type.GetType(
                "UnityEngine.Rendering.Universal.UniversalAdditionalCameraData, " +
                "Unity.RenderPipelines.Universal.Runtime");
            if (urpDataType != null && cam.GetComponent(urpDataType) != null)
            {
                var urpData = captureGo.AddComponent(urpDataType);
                var postProcessing = urpDataType.GetProperty(
                    "renderPostProcessing", BindingFlags.Instance | BindingFlags.Public);
                if (postProcessing != null && postProcessing.CanWrite)
                    postProcessing.SetValue(urpData, false, null);
            }

            captureCam.targetTexture = rt;
            yield return null;  // let the isolated camera's own normal render happen
            yield return null;  // and one more, cheap insurance against a one-frame-late clear

            RenderTexture.active = rt;
            tex.ReadPixels(new Rect(0, 0, width, height), 0, 0);
            tex.Apply();

            byte[] png = tex.EncodeToPNG();
            Assert.IsTrue(png != null && png.Length > 0, "EncodeToPNG() produced an empty file");

            string path = Path.Combine(ProjectRoot, ScreenshotFilename);
            File.WriteAllBytes(path, png);
        }
        finally
        {
            RenderTexture.active = prevActive;
            if (captureState != null)
                captureState.Restore();
            UnityEngine.Object.Destroy(captureGo);
            UnityEngine.Object.Destroy(tex);
            rt.Release();
            UnityEngine.Object.Destroy(rt);
        }
    }
}
