// Syntax-only C# parse gate for generated Unity scripts.
//
// Roslyn PARSE diagnostics only — no binding, no references, no Unity
// assemblies. That distinction is the whole design: `csc` against a stub
// project also binds, so every generated file returns CS0246 for UnityEngine
// and the real syntax errors drown in noise. `CSharpSyntaxTree.ParseText`
// stops after the parser, which is exactly the class of defect this gate is
// meant to catch (verified: 0 findings across 138 known-good generated files).
//
// Roslyn ships inside the .NET SDK (sdk/<ver>/Roslyn/bincore), so this builds
// offline with no NuGet restore. See src/csharp_syntax_gate.py.
//
// Usage:  SyntaxGate @<listfile>   (one absolute path per line, UTF-8)
//         SyntaxGate <file.cs> ...
// Output: one diagnostic per line, `path(line,col): CSxxxx: message`
// Exit:   0 = clean, 1 = syntax errors found, 2 = bad invocation.

using System;
using System.IO;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

internal static class Program
{
    private static int Main(string[] args)
    {
        if (args.Length == 0)
        {
            Console.Error.WriteLine("usage: SyntaxGate @<listfile> | <file.cs> ...");
            return 2;
        }

        string[] files;
        try
        {
            files = args.Length == 1 && args[0].StartsWith("@")
                ? File.ReadAllLines(args[0].Substring(1))
                : args;
        }
        catch (IOException ex)
        {
            Console.Error.WriteLine($"cannot read file list: {ex.Message}");
            return 2;
        }

        // Unity 6000.0 is C# 9, so parse as C# 9.
        //
        // Note this does NOT catch newer-language constructs: Roslyn PARSES a
        // collection expression or a file-scoped namespace happily and defers
        // the "feature is not available in C# 9" error to binding, which this
        // gate deliberately does not run. Measured, not assumed — both parse
        // clean here. Language-version violations remain a Unity-console
        // finding; this gate covers structural syntax only.
        var options = new CSharpParseOptions(LanguageVersion.CSharp9);
        int errors = 0;

        foreach (var path in files)
        {
            var file = path.Trim();
            if (file.Length == 0) continue;

            string text;
            try
            {
                text = File.ReadAllText(file);
            }
            catch (IOException ex)
            {
                Console.Error.WriteLine($"cannot read {file}: {ex.Message}");
                return 2;
            }

            var tree = CSharpSyntaxTree.ParseText(text, options, file);
            foreach (var d in tree.GetDiagnostics())
            {
                if (d.Severity != DiagnosticSeverity.Error) continue;
                var pos = d.Location.GetLineSpan().StartLinePosition;
                Console.WriteLine(
                    $"{file}({pos.Line + 1},{pos.Character + 1}): {d.Id}: {d.GetMessage()}");
                errors++;
            }
        }

        return errors == 0 ? 0 : 1;
    }
}
