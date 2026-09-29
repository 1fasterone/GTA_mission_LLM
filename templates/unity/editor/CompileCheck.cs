// Headless compile-verification entry point.
//
// Dropped into <project>/Assets/CompileCheck/ by `python unity_cli.py verify`
// together with CompileCheck.Editor.asmdef. The asmdef is an ISOLATED editor
// assembly (no reference to Assembly-CSharp), so this code still compiles and
// runs even when the project's own gameplay scripts fail to compile — which is
// exactly when we need the error report.
//
// Run via:
//   Unity.exe -batchmode -nographics -projectPath <p> -executeMethod CompileCheck.Run -logFile <log>
//
// (No -quit: this script exits the editor itself once compilation finishes, so
// the process exit code reflects the compile result — 1 if any errors, else 0.)
//
// NOT part of normal generated output. The verify harness removes it afterward.

using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text;
using UnityEditor;
using UnityEditor.Compilation;
using UnityEngine;

public static class CompileCheck
{
    private static readonly List<string> _errors   = new List<string>();
    private static readonly List<string> _warnings = new List<string>();
    private static string _reportPath;
    private static bool   _finished;
    private static double _deadline;

    // Safety net: if no compilation events fire (e.g. nothing to recompile),
    // give up after this many seconds and report whatever we have.
    private const double TimeoutSeconds = 240.0;

    public static void Run()
    {
        // <project>/Assets -> <project>
        string projectRoot = Directory.GetParent(Application.dataPath).FullName;
        _reportPath = Path.Combine(projectRoot, "compile_report.json");

        CompilationPipeline.assemblyCompilationFinished += OnAssemblyFinished;
        CompilationPipeline.compilationFinished         += OnCompilationFinished;

        _deadline = EditorApplication.timeSinceStartup + TimeoutSeconds;
        EditorApplication.update += OnUpdate;

        // Force a fresh compile so assemblyCompilationFinished fires for every
        // assembly, including ones that errored during the initial import.
        CompilationPipeline.RequestScriptCompilation();
    }

    private static void OnUpdate()
    {
        if (_finished) return;
        if (EditorApplication.timeSinceStartup >= _deadline)
        {
            // Timed out — most often means there was nothing to recompile
            // (project was already clean). Report what we collected and exit 0
            // unless errors were seen.
            Finish();
        }
    }

    private static void OnAssemblyFinished(string assemblyPath, CompilerMessage[] messages)
    {
        foreach (var m in messages)
        {
            string entry = Encode(m.file, m.line, m.column, m.message);
            if (m.type == CompilerMessageType.Error) _errors.Add(entry);
            else if (m.type == CompilerMessageType.Warning) _warnings.Add(entry);
        }
    }

    private static void OnCompilationFinished(object _)
    {
        Finish();
    }

    private static void Finish()
    {
        if (_finished) return;
        _finished = true;

        CompilationPipeline.assemblyCompilationFinished -= OnAssemblyFinished;
        CompilationPipeline.compilationFinished         -= OnCompilationFinished;
        EditorApplication.update                        -= OnUpdate;

        if (_errors.Count == 0)
            VerifyGeneratedGreyboxScene();

        WriteReport();

        int code = _errors.Count > 0 ? 1 : 0;
        Debug.Log("[CompileCheck] done — errors=" + _errors.Count +
                  " warnings=" + _warnings.Count + " exit=" + code);
        EditorApplication.Exit(code);
    }

    // A static Step 7 score cannot prove that editor-only generation actually
    // built a scene. If this project ships the deterministic greybox generator,
    // run its public entry point and require the saved scene.
    // Reflection keeps this isolated asmdef independent of Assembly-CSharp-
    // Editor, so ordinary compile failures still produce a useful report.
    private static void VerifyGeneratedGreyboxScene()
    {
        const string generatorName = "GreyboxLevelGenerator";
        Type generator = null;
        foreach (System.Reflection.Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
        {
            generator = assembly.GetType(generatorName, false);
            if (generator != null) break;
        }

        // This is not a greybox project; compilation is the complete gate.
        if (generator == null) return;

        MethodInfo generate = generator.GetMethod(
            "GenerateAndSavePreviewScene",
            BindingFlags.Public | BindingFlags.Static);
        if (generate == null)
        {
            _errors.Add(Encode(
                "Assets/Editor/GreyboxLevelGenerator.cs", 0, 0,
                "Greybox generator is missing public GenerateAndSavePreviewScene()."));
            return;
        }

        try
        {
            generate.Invoke(null, null);
        }
        catch (TargetInvocationException e)
        {
            Exception cause = e.InnerException ?? e;
            _errors.Add(Encode(
                "Assets/Editor/GreyboxLevelGenerator.cs", 0, 0,
                "Greybox scene generation failed: " + cause.Message));
            return;
        }
        catch (Exception e)
        {
            _errors.Add(Encode(
                "Assets/Editor/GreyboxLevelGenerator.cs", 0, 0,
                "Greybox scene generation failed: " + e.Message));
            return;
        }

        string scenePath = Path.Combine(
            Directory.GetParent(Application.dataPath).FullName,
            "Assets", "Scenes", "Main.unity");
        if (!File.Exists(scenePath))
        {
            _errors.Add(Encode(
                "Assets/Scenes/Main.unity", 0, 0,
                "Greybox generator completed without saving Assets/Scenes/Main.unity."));
        }
    }

    private static void WriteReport()
    {
        var sb = new StringBuilder();
        sb.Append("{\n");
        sb.Append("  \"ok\": ").Append(_errors.Count == 0 ? "true" : "false").Append(",\n");
        sb.Append("  \"error_count\": ").Append(_errors.Count).Append(",\n");
        sb.Append("  \"warning_count\": ").Append(_warnings.Count).Append(",\n");
        AppendArray(sb, "errors", _errors);
        sb.Append(",\n");
        AppendArray(sb, "warnings", _warnings);
        sb.Append("\n}\n");

        try { File.WriteAllText(_reportPath, sb.ToString()); }
        catch (System.Exception e) { Debug.LogError("[CompileCheck] write failed: " + e.Message); }
    }

    private static void AppendArray(StringBuilder sb, string key, List<string> items)
    {
        sb.Append("  \"").Append(key).Append("\": [");
        for (int i = 0; i < items.Count; i++)
        {
            sb.Append(i == 0 ? "\n    " : ",\n    ");
            sb.Append(items[i]);
        }
        sb.Append(items.Count > 0 ? "\n  ]" : "]");
    }

    // Build one JSON object literal for a compiler message.
    private static string Encode(string file, int line, int column, string message)
    {
        return "{\"file\": \"" + JsonEscape(file) +
               "\", \"line\": " + line +
               ", \"column\": " + column +
               ", \"message\": \"" + JsonEscape(message) + "\"}";
    }

    private static string JsonEscape(string s)
    {
        if (string.IsNullOrEmpty(s)) return "";
        var sb = new StringBuilder(s.Length + 8);
        foreach (char c in s)
        {
            switch (c)
            {
                case '\\': sb.Append("\\\\"); break;
                case '"':  sb.Append("\\\""); break;
                case '\n': sb.Append("\\n");  break;
                case '\r': sb.Append("\\r");  break;
                case '\t': sb.Append("\\t");  break;
                default:
                    if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                    else sb.Append(c);
                    break;
            }
        }
        return sb.ToString();
    }
}
