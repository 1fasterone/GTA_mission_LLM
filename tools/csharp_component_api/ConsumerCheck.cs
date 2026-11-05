// Candidate binding uses real supplied references. Never invent framework stubs
// or suppress compiler errors to manufacture approval of an extension.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

internal static class ConsumerCheck
{
    private static readonly SymbolDisplayFormat Display = SymbolDisplayFormat.CSharpErrorMessageFormat;

    private static object Site(CSharpCompilation compilation, Diagnostic diagnostic)
    {
        var location = diagnostic.Location;
        if (!location.IsInSource) return null;
        var model = compilation.GetSemanticModel(location.SourceTree);
        var node = location.SourceTree.GetRoot().FindNode(location.SourceSpan, getInnermostNodeForTie: true);
        var access = node.AncestorsAndSelf().OfType<MemberAccessExpressionSyntax>().FirstOrDefault();
        var binding = node.AncestorsAndSelf().OfType<MemberBindingExpressionSyntax>().FirstOrDefault();
        // Conversion diagnostics often point at the assignment RHS. Include the
        // LHS receiver instead of incorrectly dropping such diagnostics.
        var assignment = node.AncestorsAndSelf().OfType<AssignmentExpressionSyntax>().FirstOrDefault();
        if (assignment != null && (access == null && binding == null || diagnostic.Id == "CS0029" || diagnostic.Id == "CS0266"))
        {
            access = assignment.Left.DescendantNodesAndSelf().OfType<MemberAccessExpressionSyntax>().FirstOrDefault();
            binding = assignment.Left.DescendantNodesAndSelf().OfType<MemberBindingExpressionSyntax>().FirstOrDefault();
        }
        ExpressionSyntax receiver = access?.Expression;
        SimpleNameSyntax member = access?.Name ?? binding?.Name;
        if (receiver == null && binding != null)
            receiver = binding.Ancestors().OfType<ConditionalAccessExpressionSyntax>().FirstOrDefault()?.Expression;
        var receiverType = receiver == null ? null : model.GetTypeInfo(receiver).Type;
        var symbol = member == null ? null : model.GetSymbolInfo(member).Symbol;
        var span = location.GetLineSpan();
        return new {
            text = node.ToFullString(),
            receiver = receiver?.ToString() ?? "",
            receiver_type = receiverType?.ToDisplayString(Display) ?? "",
            member = member?.Identifier.ValueText ?? "",
            symbol = symbol?.ToDisplayString(Display) ?? "",
            enclosing_type = model.GetEnclosingSymbol(location.SourceSpan.Start)?.ContainingType?.ToDisplayString(Display) ?? "",
            line_text = location.SourceTree.GetText().Lines[span.StartLinePosition.Line].ToString(),
            assignment_target = assignment?.Left.ToString() ?? "",
            assignment_value = assignment?.Right.ToString() ?? ""
        };
    }

    public static int Run(JsonElement request)
    {
        var defines = request.GetProperty("defines").EnumerateArray().Select(v => v.GetString())
            .OrderBy(v => v, StringComparer.Ordinal).ToArray();
        var options = new CSharpParseOptions(LanguageVersion.CSharp9, preprocessorSymbols: defines);
        var trees = request.GetProperty("sources").EnumerateArray().Select(s => CSharpSyntaxTree.ParseText(
            s.GetProperty("text").GetString(), options, s.GetProperty("path").GetString()))
            .OrderBy(t => t.FilePath, StringComparer.Ordinal).ToArray();
        var supplied = request.GetProperty("references").EnumerateArray().Select(v => v.GetString()).ToArray();
        // An explicit reference profile owns its core library too. Mixing the
        // host runtime CoreLib into .NETStandard/Unity/reference-pack profiles
        // makes even valid predefined types fail to bind.
        var paths = (supplied.Length == 0 ? new[] { typeof(object).Assembly.Location } : supplied)
            .Distinct(StringComparer.Ordinal).OrderBy(v => v, StringComparer.Ordinal).ToArray();
        var references = paths.Select(p => MetadataReference.CreateFromFile(p)).ToArray();
        var compilation = CSharpCompilation.Create("CandidateComponents", trees, references,
            new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary, deterministic: true));
        var all = compilation.GetDiagnostics().OrderBy(d => d.Location.SourceTree?.FilePath, StringComparer.Ordinal)
            .ThenBy(d => d.Location.SourceSpan.Start).ThenBy(d => d.Id, StringComparer.Ordinal).ToArray();
        var diagnostics = all.Select(d => new {
            code = d.Id, severity = d.Severity.ToString().ToLowerInvariant(),
            message = d.GetMessage(CultureInfo.InvariantCulture),
            path = d.Location.SourceTree?.FilePath ?? "",
            line = d.Location.IsInSource ? d.Location.GetLineSpan().StartLinePosition.Line + 1 : 0,
            column = d.Location.IsInSource ? d.Location.GetLineSpan().StartLinePosition.Character + 1 : 0,
            start = d.Location.SourceSpan.Start, length = d.Location.SourceSpan.Length,
            callsite = Site(compilation, d)
        }).ToArray();
        var failed = all.Any(d => d.Severity == DiagnosticSeverity.Error);
        Console.WriteLine(JsonSerializer.Serialize(new {
            format = "csharp-consumer-check/v1", language = "CSharp9", defines,
            roslyn = typeof(CSharpCompilation).Assembly.GetName().Version.ToString(),
            accepted = !failed, diagnostics,
            reference_scope = supplied.Length == 0
                ? "SDK core runtime only; no implicit Unity references"
                : "explicit reference profile; no implicit runtime or Unity references",
            core_runtime = new { name = typeof(object).Assembly.GetName().Name,
                version = typeof(object).Assembly.GetName().Version.ToString(),
                used_as_compilation_reference = supplied.Length == 0 },
            unity_editor_execution = "unverified"
        }));
        return failed ? 1 : 0;
    }
}
