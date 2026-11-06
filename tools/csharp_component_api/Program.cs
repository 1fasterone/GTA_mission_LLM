// Roslyn declaration extraction. Framework binding/Unity compilation is separate.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

internal static class Program
{
    private static readonly SymbolDisplayFormat TypeFormat = new SymbolDisplayFormat(
        globalNamespaceStyle: SymbolDisplayGlobalNamespaceStyle.Omitted,
        typeQualificationStyle: SymbolDisplayTypeQualificationStyle.NameAndContainingTypesAndNamespaces,
        genericsOptions: SymbolDisplayGenericsOptions.IncludeTypeParameters,
        miscellaneousOptions: SymbolDisplayMiscellaneousOptions.UseSpecialTypes |
            SymbolDisplayMiscellaneousOptions.IncludeNullableReferenceTypeModifier |
            SymbolDisplayMiscellaneousOptions.EscapeKeywordIdentifiers);
    private static string TypeName(ITypeSymbol t) => t?.ToDisplayString(TypeFormat) ?? "";
    private static string Access(ISymbol s) => s.DeclaredAccessibility.ToString().ToLowerInvariant();
    private static object Location(ISymbol s)
    {
        var loc = s.Locations.FirstOrDefault(l => l.IsInSource);
        if (loc == null) return null;
        var span = loc.GetLineSpan();
        return new { path = span.Path, line = span.StartLinePosition.Line + 1, column = span.StartLinePosition.Character + 1 };
    }
    private static object Parameter(IParameterSymbol p)
    {
        var syntax = p.DeclaringSyntaxReferences.FirstOrDefault()?.GetSyntax() as ParameterSyntax;
        return new { name = p.Name, type = TypeName(p.Type), modifier = p.RefKind.ToString().ToLowerInvariant(),
            modifiers = syntax?.Modifiers.Select(t => t.Text).ToArray() ?? Array.Empty<string>(),
            optional = p.IsOptional, @params = p.IsParams,
            @default = syntax?.Default?.Value.NormalizeWhitespace().ToFullString() };
    }
    private static object[] Parameters(IEnumerable<IParameterSymbol> p) => p.Select(Parameter).ToArray();
    private static object Accessor(IMethodSymbol a) => a == null ? null : new { accessibility = Access(a), init_only = a.IsInitOnly };
    private static object Member(ISymbol s)
    {
        string kind, type = "", methodKind = "", refKind = "none";
        int arity = 0;
        object[] parameters = Array.Empty<object>();
        object getter = null, setter = null;
        if (s is IMethodSymbol m)
        {
            kind = "method"; type = TypeName(m.ReturnType); arity = m.Arity; parameters = Parameters(m.Parameters);
            methodKind = m.MethodKind.ToString().ToLowerInvariant(); refKind = m.RefKind.ToString().ToLowerInvariant();
        }
        else if (s is IPropertySymbol p)
        {
            kind = p.IsIndexer ? "indexer" : "property"; type = TypeName(p.Type); parameters = Parameters(p.Parameters);
            getter = Accessor(p.GetMethod); setter = Accessor(p.SetMethod); refKind = p.RefKind.ToString().ToLowerInvariant();
        }
        else if (s is IEventSymbol e)
        {
            kind = "event"; type = TypeName(e.Type);
            if (e.Type is INamedTypeSymbol d && d.DelegateInvokeMethod != null) parameters = Parameters(d.DelegateInvokeMethod.Parameters);
        }
        else if (s is IFieldSymbol f) { kind = "field"; type = TypeName(f.Type); }
        else return null;
        return new { id = s.GetDocumentationCommentId() ?? s.ToDisplayString(), name = s.Name,
            kind, accessibility = Access(s), is_static = s.IsStatic, is_implicit = s.IsImplicitlyDeclared,
            type, generic_arity = arity, parameters, method_kind = methodKind, ref_kind = refKind, getter, setter,
            is_readonly = s is IFieldSymbol field && (field.IsReadOnly || field.IsConst),
            explicit_interfaces = s is IMethodSymbol method ? method.ExplicitInterfaceImplementations.Select(i => i.ToDisplayString()).OrderBy(v => v, StringComparer.Ordinal).ToArray() : Array.Empty<string>(),
            location = Location(s) };
    }
    private static IEnumerable<INamedTypeSymbol> Types(INamespaceOrTypeSymbol parent)
    {
        foreach (var symbol in parent.GetMembers().OrderBy(s => s.Name, StringComparer.Ordinal))
        {
            if (symbol is INamespaceSymbol space) foreach (var nested in Types(space)) yield return nested;
            else if (symbol is INamedTypeSymbol type)
            {
                yield return type;
                foreach (var nested in Types(type)) yield return nested;
            }
        }
    }
    private static object Base(INamedTypeSymbol t) => new { type = TypeName(t), definition = TypeName(t.OriginalDefinition),
        scope = t.Locations.Any(l => l.IsInSource) ? "project" : "external", unresolved = t.TypeKind == TypeKind.Error };
    private static int Main()
    {
        try
        {
            using var request = JsonDocument.Parse(Console.In.ReadToEnd());
            var root = request.RootElement;
            if (root.TryGetProperty("action", out var action) && action.GetString() == "check")
                return ConsumerCheck.Run(root);
            var defines = root.TryGetProperty("defines", out var d) ? d.EnumerateArray().Select(v => v.GetString()).OrderBy(s => s, StringComparer.Ordinal).ToArray() : Array.Empty<string>();
            var options = new CSharpParseOptions(LanguageVersion.CSharp9, preprocessorSymbols: defines);
            var trees = root.GetProperty("sources").EnumerateArray().Select(s => CSharpSyntaxTree.ParseText(
                s.GetProperty("text").GetString(), options, s.GetProperty("path").GetString())).OrderBy(t => t.FilePath, StringComparer.Ordinal).ToArray();
            var compilation = CSharpCompilation.Create("ComponentDeclarations", trees,
                new[] { MetadataReference.CreateFromFile(typeof(object).Assembly.Location) }, new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary));
            // Roslyn unifies duplicate type symbols. Keep declaration conflicts
            // visible before exporting a union that no emitted file can own.
            // Other binding errors require the real Unity references/Editor.
            var declarationErrors = compilation.GetDeclarationDiagnostics().Where(x =>
                new[] { "CS0101", "CS0102", "CS0111", "CS0260", "CS0261", "CS0262", "CS0263", "CS0264", "CS0265" }.Contains(x.Id));
            var diagnostics = trees.SelectMany(t => t.GetDiagnostics()).Concat(declarationErrors).Where(x => x.Severity == DiagnosticSeverity.Error)
                .OrderBy(x => x.Location.SourceTree?.FilePath, StringComparer.Ordinal).ThenBy(x => x.Location.SourceSpan.Start).ThenBy(x => x.Id, StringComparer.Ordinal)
                .Select(x => new { code = x.Id, message = x.GetMessage(System.Globalization.CultureInfo.InvariantCulture), path = x.Location.SourceTree?.FilePath ?? "",
                    line = x.Location.GetLineSpan().StartLinePosition.Line + 1, column = x.Location.GetLineSpan().StartLinePosition.Character + 1 }).ToArray();
            var types = Types(compilation.Assembly.GlobalNamespace).Where(t => !t.IsImplicitlyDeclared && t.Locations.Any(l => l.IsInSource))
                .OrderBy(TypeName, StringComparer.Ordinal).Select(t => new {
                    id = TypeName(t), name = t.Name, kind = t.TypeKind.ToString().ToLowerInvariant(), accessibility = Access(t), is_static = t.IsStatic,
                    generic_arity = t.Arity, containing_type = t.ContainingType == null ? "" : TypeName(t.ContainingType),
                    bases = (t.BaseType == null ? Array.Empty<INamedTypeSymbol>() : new[] { t.BaseType }).Concat(t.Interfaces).OrderBy(TypeName, StringComparer.Ordinal).Select(Base).ToArray(),
                    members = t.GetMembers().Where(m => (!m.IsImplicitlyDeclared || m.DeclaredAccessibility == Accessibility.Public) &&
                        !(m is INamedTypeSymbol) && !(m is IMethodSymbol mm && mm.AssociatedSymbol != null)).Select(Member).Where(m => m != null)
                        .OrderBy(m => JsonSerializer.Serialize(m), StringComparer.Ordinal).ToArray(), location = Location(t) }).ToArray();
            Console.WriteLine(JsonSerializer.Serialize(new { format = "csharp-declarations/v1", roslyn = typeof(CSharpCompilation).Assembly.GetName().Version.ToString(),
                language = "CSharp9", defines, diagnostics, types, semantic_compilation = "unverified; framework references are not supplied" }));
            return diagnostics.Length == 0 ? 0 : 1;
        }
        catch (Exception e) { Console.Error.WriteLine($"[component-api] {e.GetType().Name}: {e.Message}"); return 2; }
    }
}
