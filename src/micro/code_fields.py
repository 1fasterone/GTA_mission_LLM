"""
Typed code-block primitives for prompt-specific behavior in micro generators.

Why this exists:
The micro pipelines were producing identical scripts regardless of prompt
because Step 6 (Jinja render) could only express what the structural schema
modeled. Schemas like ControllerSchema had `variant`, `move_speed`, etc., but
nothing for "oxygen meter" or "shift-to-boost." The LLM's concept was
captured but never reached the file.

Solution: the LLM now authors typed C# fragments alongside structural fields.
`CodeField` declares a `[SerializeField]`; `CodeMethod` declares a method
signature and body. The Jinja templates have named injection points that
render these fragments into the produced .cs file.

LLMs author imperfect C#, so every fragment is run through `lint_*` before
being handed to Jinja. The lint pass is deliberately defensive — it never
raises, it cleans what it can, drops what it can't, and logs every fix.
"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from rich.console import Console

console = Console()


# ── Schema primitives ─────────────────────────────────────────────────────────

# C# identifier — must start with a letter or _, followed by letters/digits/_.
_CSHARP_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Allowed `type` shapes: bare identifiers (float, MyType), generics (List<int>),
# arrays (int[]), and dotted refs (UnityEngine.Vector3). Defensive whitelist.
_CSHARP_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.<>,\[\]?\s]*$")


class CodeField(BaseModel):
    """An LLM-authored [SerializeField] declaration."""
    name:    str
    type:    str = "float"
    default: Optional[str] = None
    header:  Optional[str] = None
    tooltip: Optional[str] = None

    @field_validator("name", mode="before")
    @classmethod
    def _check_name(cls, v):
        s = str(v).strip()
        if not _CSHARP_IDENT_RE.match(s):
            raise ValueError(f"Invalid C# identifier: {s!r}")
        return s

    @field_validator("type", mode="before")
    @classmethod
    def _check_type(cls, v):
        s = str(v).strip() or "float"
        if not _CSHARP_TYPE_RE.match(s):
            return "float"  # graceful fallback rather than reject the whole schema
        return s

    @field_validator("default", "header", "tooltip", mode="before")
    @classmethod
    def _coerce_str(cls, v):
        # Models sometimes emit a number/bool for `default` (e.g. 0 instead of
        # "0"). These render into C# as literals, so coerce to str rather than
        # crashing schema validation. C# booleans are lower-case.
        if v is None:
            return None
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)


class CodeMethod(BaseModel):
    """An LLM-authored helper method appended to a generated class."""
    signature: str
    body:      str = ""

    @model_validator(mode="before")
    @classmethod
    def _accept_aliases(cls, data):
        """Tolerate the common LLM shape drift for method entries. The schema
        wants {signature, body}; LLMs frequently emit {name, body} or
        {signature, code}. Map the aliases so one stray key doesn't abort the
        whole schema-fill (valid {signature, body} input passes through
        untouched)."""
        if not isinstance(data, dict):
            return data
        d = dict(data)
        if not d.get("signature"):
            for alt in ("name", "sig", "declaration", "header", "method_signature"):
                if d.get(alt):
                    d["signature"] = d[alt]
                    break
        if not d.get("body"):
            for alt in ("code", "method", "impl", "content", "method_body"):
                if d.get(alt):
                    d["body"] = d[alt]
                    break
        return d

    @field_validator("signature", mode="before")
    @classmethod
    def _normalize_signature(cls, v):
        s = str(v).strip()
        # Drop any trailing `{` or `;` — the template adds the brace itself.
        s = s.rstrip("{ ;")
        # A bare method name (e.g. "OnAirEmpty") is not a valid C# signature —
        # the template renders `<signature>\n{ ... }`. Synthesise a private void
        # header so the method compiles instead of becoming a syntax error.
        if s and "(" not in s and _CSHARP_IDENT_RE.match(s):
            s = f"private void {s}()"
        return s


# ── Lint helpers ──────────────────────────────────────────────────────────────

# Markdown fences the LLM sometimes wraps around code blocks.
_MD_FENCE_RE = re.compile(r"^\s*```\s*[A-Za-z]*\s*$", re.MULTILINE)
# Heuristic Python tokens — if any of these appear, the fragment is almost
# certainly the wrong language.
_PY_TOKENS = (
    "def ", "lambda ", "print(", "import ", "elif ", "self.", "None", "True",
    "False", "__init__", "raise Exception",
)
# Conservative C# `using` line — anything that mentions a dotted namespace.
_USING_RE = re.compile(r"^\s*using\s+[\w.]+\s*;\s*$", re.MULTILINE)

# Methods longer than this get truncated with a TODO marker.
_MAX_METHOD_LINES = 60


def _strip_md_fences(text: str) -> str:
    return _MD_FENCE_RE.sub("", text)


def _looks_pythonic(text: str) -> bool:
    return any(tok in text for tok in _PY_TOKENS)


def _balance_braces(text: str) -> str:
    """Auto-close unbalanced { } with appended close-braces. Never reduces."""
    open_n  = text.count("{")
    close_n = text.count("}")
    if open_n > close_n:
        text = text + ("\n" + "}" * (open_n - close_n))
    return text


def _cap_lines(text: str, *, what: str) -> str:
    lines = text.splitlines()
    if len(lines) <= _MAX_METHOD_LINES:
        return text
    kept = lines[: _MAX_METHOD_LINES - 1]
    kept.append(f"// TODO: continue — {what} truncated at {_MAX_METHOD_LINES} lines")
    return "\n".join(kept)


# CS1612: a value-type property returns a COPY, so mutating a member of it
# (`transform.position.x += v`, `Physics.gravity.y = v`) does not compile. LLMs
# write this constantly. Repair to read-modify-write on a local.
_STRUCT_MEMBER_ASSIGN_RE = re.compile(
    r'((?:[A-Za-z_]\w*\.)*'
    r'(?:transform\.(?:position|localPosition|localScale|eulerAngles|localEulerAngles)'
    r'|Physics2D\.gravity|Physics\.gravity))'
    r'\.([xyzw])\s*(\+=|-=|\*=|/=|=(?!=))\s*([^;]+?)\s*;')


def _repair_struct_member_assign(text: str) -> str:
    """Rewrite `prop.x += v;` → `{ var _v = prop; _v.x += v; prop = _v; }` for the
    common value-type Unity properties that otherwise throw CS1612."""
    new = _STRUCT_MEMBER_ASSIGN_RE.sub(r'{ var _v = \1; _v.\2 \3 \4; \1 = _v; }', text)
    if new != text:
        console.print("  [magenta]FIX[/]  rewrote value-type property member assignment (CS1612)")
    return new


# CS0103: LLMs write the pre-C#7 raycast idiom `Physics.Raycast(ray, out hit, …)`
# without ever declaring `hit`. Modern C# allows an inline `out var hit`, so
# declare the out parameter in place. Only lowercase, non-field (`_`-prefixed)
# identifiers that aren't already declared elsewhere in the fragment are
# touched; a second `out hit` reuses the first's declaration.
_OUT_UNDECLARED_RE = re.compile(r'\bout\s+(?!var\b|ref\b)([a-z][A-Za-z0-9_]*)\b')


def _declare_inline_out_params(text: str) -> str:
    seen: set[str] = set()
    changed = False

    def repl(m: "re.Match") -> str:
        nonlocal changed
        ident = m.group(1)
        if ident in seen:
            return m.group(0)                       # reuse the first declaration
        # Already declared explicitly in this fragment (`var hit` / `RaycastHit
        # hit`)? Match `var`, a primitive, or a Capitalized type name before the
        # identifier — deliberately NOT the `out`/`ref` keyword itself (lowercase,
        # non-primitive), which would otherwise look like a declaration.
        if re.search(rf'\b(?:var|int|float|double|bool|string|byte|long|short|'
                     rf'char|object|[A-Z][\w<>\[\].]*)\s+{re.escape(ident)}\b', text):
            return m.group(0)
        seen.add(ident)
        changed = True
        return f'out var {ident}'

    new = _OUT_UNDECLARED_RE.sub(repl, text)
    if changed:
        console.print("  [magenta]FIX[/]  declared inline out parameter(s) (CS0103)")
    return new


def lint_code_fragment(text: str, *, what: str) -> str:
    """
    Clean an LLM-authored C# fragment intended for an injection point.
    Returns "" if the fragment is rejected (caller should skip it).
    """
    if not text:
        return ""
    text = _strip_md_fences(text).strip()
    if not text:
        return ""
    if _looks_pythonic(text):
        console.print(f"  [yellow]FIX[/]  rejected Python-shaped {what}")
        return ""
    # Strip `using X;` lines — these belong in `extra_usings`, not the body.
    text = _USING_RE.sub("", text)
    text = _repair_struct_member_assign(text)
    text = _declare_inline_out_params(text)
    text = _balance_braces(text)
    text = _cap_lines(text, what=what)
    return text.strip()


def lint_method(m: CodeMethod, *, taken_names: set[str]) -> Optional[CodeMethod]:
    """
    Validate + clean a CodeMethod. Returns None if it should be dropped.
    `taken_names` is mutated with the method's lower-cased identifier so
    duplicates are dropped.
    """
    sig  = m.signature.strip()
    body = lint_code_fragment(m.body, what=f"method {sig!r}")
    if not body:
        return None
    # Extract method name for dedupe (best-effort; brittle parse is OK here).
    name_match = re.search(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", sig)
    if name_match:
        key = name_match.group(1).lower()
        if key in taken_names:
            console.print(f"  [yellow]FIX[/]  dropped duplicate method {key!r}")
            return None
        taken_names.add(key)
    return CodeMethod(signature=sig, body=body)


# Namespaces a generated MonoBehaviour can legitimately `using`. Models put TYPE
# names here too (`using UnityEngine.AudioSource;` → CS0138, hit live on the
# SunsetDriftKingdom beta run), so anything outside this allowlist is dropped.
_KNOWN_NAMESPACES = {
    "System", "System.Collections", "System.Collections.Generic", "System.Linq",
    "System.Text", "System.IO", "System.Threading.Tasks",
    "UnityEngine", "UnityEngine.UI", "UnityEngine.UIElements",
    "UnityEngine.InputSystem", "UnityEngine.AI", "UnityEngine.Audio",
    "UnityEngine.Events", "UnityEngine.SceneManagement", "UnityEngine.Rendering",
    "UnityEngine.Rendering.Universal", "UnityEngine.Animations", "UnityEngine.Tilemaps",
    "UnityEngine.U2D", "UnityEngine.Playables", "UnityEngine.Video",
    "TMPro", "Unity.Cinemachine", "Cinemachine",
}

# Content patterns → the namespace they require. If a fragment/field uses the
# pattern and the using is missing, we add it instead of letting compile fail
# (`List<Light>` without System.Collections.Generic was hit live; a custom
# script referencing bare `InputSystem.*`/`InputAction` without
# `UnityEngine.InputSystem` — CS0103 "the name 'InputSystem' does not exist in
# the current context" — and `Resources.Load<SpriteAtlas>` without
# `UnityEngine.U2D` — CS0311 — were both hit live too).
_USING_TRIGGERS: list[tuple[str, str]] = [
    (r"\b(?:List|Dictionary|HashSet|Queue|Stack)\s*<",     "System.Collections.Generic"),
    (r"\b(?:InputSystem|InputAction|InputActionAsset|InputActionMap|"
     r"InputActionReference|Keyboard|Mouse|Gamepad|Touchscreen)\b",   "UnityEngine.InputSystem"),
    (r"\bNavMeshAgent\b",                                  "UnityEngine.AI"),
    (r"\bUnityEvent\b",                                    "UnityEngine.Events"),
    (r"\bSceneManager\s*\.",                               "UnityEngine.SceneManagement"),
    (r"\bTMP_\w+|\bTextMeshPro",                           "TMPro"),
    (r"\bIEnumerator\b",                                   "System.Collections"),
    (r"\bSpriteAtlas\b",                                   "UnityEngine.U2D"),
    (r"\b(?:Tilemap|TileBase)\b",                          "UnityEngine.Tilemaps"),
    (r"\bPlayableDirector\b",                               "UnityEngine.Playables"),
    (r"\bVideoPlayer\b",                                    "UnityEngine.Video"),
    (r"\bTask\s*[<(]|\bTask\.(?:Run|Delay|WhenAll|WhenAny)\b", "System.Threading.Tasks"),
]


def lint_usings(usings: list[str], *, already_in_template: set[str],
                allowed: set[str] | None = None) -> list[str]:
    """Dedupe, drop template-emitted entries, keep stable order.

    `allowed` (e.g. _KNOWN_NAMESPACES for Unity) additionally drops anything
    outside the set — models put TYPE names in usings (`UnityEngine.AudioSource`
    → CS0138). None = permissive (the GTA domain allows arbitrary mod namespaces).
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in usings:
        s = str(raw).strip().rstrip(";").strip()
        if not s:
            continue
        # Accept either "X.Y.Z" or "using X.Y.Z" forms.
        if s.startswith("using "):
            s = s[len("using "):]
        if not re.match(r"^[\w][\w.]*$", s):
            continue
        if allowed is not None and s not in allowed:
            console.print(f"  [yellow]FIX[/]  dropped non-namespace using {s!r}")
            continue
        if s in already_in_template or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


# Matches `Hash.SOME_NATIVE_NAME` references in an LLM-authored C# fragment.
_HASH_REF_RE = re.compile(r'\bHash\.([A-Z][A-Z0-9_]*)\b')


def _strip_trailing_comment(line: str) -> str:
    """Best-effort strip of a trailing ` // ...` inline comment so
    _safe_to_comment_out can see the CODE ends in `;`/`}` even when followed
    by one (confirmed live: `Task.GoTo(...); // Initial nudge`, a real
    corpus line, was wrongly skipped as "unsafe" before this). Deliberately
    conservative: only strips when `//` is preceded by whitespace -- an
    inline comment almost always is, while `//` inside a string literal like
    "http://x" never has a space before it. A wrong guess here only makes
    this MORE conservative (skip a line that was actually safe), never less
    safe, so a simple heuristic is fine -- this is not a real tokenizer."""
    idx = line.find(" //")
    return line[:idx] if idx != -1 else line


def _safe_to_comment_out(line: str) -> bool:
    """A line is safe to replace with a `//` comment only if it's a complete,
    self-terminating statement: its own brace count is balanced AND it ends
    in `;` or `}` (closing a fully self-contained statement/block on the same
    line, ignoring any trailing inline comment). A bare control-flow header
    (`if (cond)`, `while (cond)`, ...) whose `{` opens on a LATER line must
    never be commented out here -- doing so would silently turn a
    conditional block into an always-executing bare block (or an orphaned
    brace), which is a correctness bug worse than the compile error the line
    would otherwise surface. Confirmed live: an LLM-authored `if (...
    GetNearestVehicle...)` header (brace on the next line) would have been
    silently neutered into an unconditional block before this guard was
    added."""
    code = _strip_trailing_comment(line)
    stripped = code.rstrip()
    if code.count("{") != code.count("}"):
        return False
    return stripped.endswith((";", "}"))


# A bare if/else-if/while header (condition only, `{` opens on a later line)
# whose condition references a bad identifier can't be commented out, but its
# condition CAN always be swapped for the literal `false` -- syntactically
# valid regardless of what the original expression was, and always disables
# the guarded block without touching any other line's braces. Restricted to
# if/while: for/foreach/switch conditions aren't simple boolean swaps, so
# those are left alone (skipped) for the compiler to catch.
_DISABLEABLE_HEADER_RE = re.compile(
    r'^(?P<indent>\s*)(?P<prefix>(?:\}\s*)?(?:else\s+)?(?:if|while))\s*\((?P<cond>.*)\)\s*$')


def _disable_bad_header(line: str, names: "list[str] | set[str]") -> "str | None":
    """Return `line` with an if/else-if/while condition replaced by `false`,
    or None if `line` isn't a bare header of that shape (e.g. has a trailing
    `{`/`;`, or is a for/foreach/switch -- those are left for the caller to
    handle via the normal comment-out / skip path)."""
    m = _DISABLEABLE_HEADER_RE.match(line)
    if not m:
        return None
    return (f"{m.group('indent')}{m.group('prefix')} (false) "
            f"/* AUDIT: condition removed, referenced invalid "
            f"{', '.join(sorted(set(names)))} */")


def lint_hash_references(text: str, *, allowed: "frozenset[str] | set[str] | None") -> str:
    """Neutralize any line referencing an unknown GTA.Native.Hash member.

    LLMs invent plausible-looking native names (SET_MISSION_FAILED,
    SET_MISSION_PASSED, ...) that don't exist in GTA.Native.Hash and fail
    CS0117 at compile time -- the #1 real-world compile failure in
    generated SHVDN3 scripts (confirmed via the SHVDN3 audit tool). This
    comments out the offending line entirely rather than half-editing it:
    a missing side effect beats a script that won't compile.

    `allowed=None` is a no-op (skips the check, e.g. when no allowlist is
    loaded). Line-based, not a real parser -- assumes one statement per
    physical line, which matches how LLMs emit Function.Call(...)
    fragments in this project's few-shot examples. See
    `_safe_to_comment_out` for when a flagged line is left alone instead.
    """
    if not text or allowed is None:
        return text
    out: list[str] = []
    flagged: set[str] = set()
    skipped: set[str] = set()
    for line in text.splitlines():
        if line.lstrip().startswith("//"):
            # Already neutralized by an earlier lint pass (its "// AUDIT:"
            # comment preserves the original text for traceability, which
            # would otherwise re-trigger this same regex on every later pass).
            out.append(line)
            continue
        bad = [n for n in _HASH_REF_RE.findall(line) if n not in allowed]
        if not bad:
            out.append(line)
            continue
        if not _safe_to_comment_out(line):
            disabled = _disable_bad_header(line, bad)
            if disabled is not None:
                flagged.update(bad)
                out.append(disabled)
                continue
            skipped.update(bad)
            out.append(line)
            continue
        flagged.update(bad)
        indent = line[: len(line) - len(line.lstrip())]
        out.append(f"{indent}// AUDIT: removed invalid Hash member(s) "
                   f"{', '.join(sorted(set(bad)))} -- {line.strip()}")
    if flagged:
        console.print(f"  [yellow]FIX[/]  removed statement(s) referencing "
                      f"invalid Hash member(s): {', '.join(sorted(flagged))}")
    if skipped:
        console.print(f"  [yellow]WARN[/] could not safely remove invalid Hash "
                      f"member(s) on a control-flow or brace-mixed line: "
                      f"{', '.join(sorted(skipped))}")
    return "\n".join(out)


# ── Type-aware member reference checker ───────────────────────────────────────
# LLMs invent plausible-but-wrong C# members on known SHVDN3 types just as
# readily as they invent Hash native names (Ped.GetNearestVehicle,
# Ped.SetPositionAndRotation, ...) -- both are the same failure mode
# (confident hallucination of API surface) on a different attack surface.
# Mirrors a battle-tested SHVDN3 audit tool's approach: type inference is
# deliberately conservative -- only fires on an explicit local declaration
# (`Ped p = ...`) or one of a few unambiguous chain/static patterns, never
# guesses across assignment from an unknown expression, and skips a variable
# name bound to two different types in the same fragment rather than risk a
# false accusation.

_MEMBER_VAR_TYPES = {
    "Ped": "GTA.Ped", "Vehicle": "GTA.Vehicle", "Weapon": "GTA.Weapon",
    "Model": "GTA.Model", "RaycastResult": "GTA.RaycastResult",
    "Prop": "GTA.Prop", "Blip": "GTA.Blip", "Camera": "GTA.Camera",
    "Player": "GTA.Player",
}
_MEMBER_VAR_DECL_RE = re.compile(
    r'\b(' + "|".join(_MEMBER_VAR_TYPES) + r')\s+([A-Za-z_]\w*)\b(?=\s*[=,;)\]])')

# Chain patterns that identify a type without needing a declared variable.
# Each yields (regex, type_fqn) — the regex's single capture group is the member.
_MEMBER_CHAIN_PATTERNS = [
    (re.compile(r'\bGame\.Player\.Character\.([A-Za-z_]\w*)\b'), "GTA.Ped"),
    (re.compile(r'\.Task\.([A-Za-z_]\w*)\b'), "GTA.TaskInvoker"),
    (re.compile(r'\.Weapons\.([A-Za-z_]\w*)\b'), "GTA.WeaponCollection"),
]
# Static classes checked directly (top-level member only).
_MEMBER_STATIC_PATTERNS = [
    (re.compile(r'(?<!\w)World\.([A-Za-z_]\w*)\b'), "GTA.World"),
    (re.compile(r'(?<!\w)Game\.([A-Za-z_]\w*)\b'), "GTA.Game"),
]

# Object-level members every type has; never indicate a fake member.
_MEMBER_ALWAYS_OK = {"ToString", "Equals", "GetHashCode", "GetType", "ReferenceEquals"}
# A surface smaller than this is untrustworthy (a thin/partial extraction would
# cause false positives on every use) — never check against it.
_MIN_MEMBER_SURFACE = 5


def _infer_member_var_types(source: str) -> dict[str, str]:
    """Return {var_name: type_fqn}, dropping any name bound to more than one
    distinct type in this fragment (ambiguous — safer to skip than misattribute)."""
    seen: dict[str, str] = {}
    ambiguous: set[str] = set()
    for m in _MEMBER_VAR_DECL_RE.finditer(source):
        keyword, name = m.group(1), m.group(2)
        fqn = _MEMBER_VAR_TYPES[keyword]
        if name in seen and seen[name] != fqn:
            ambiguous.add(name)
        else:
            seen[name] = fqn
    for name in ambiguous:
        seen.pop(name, None)
    return seen


def find_invalid_member_refs(source: str, *, surface: dict) -> list[tuple[int, str, str]]:
    """Return [(char_offset, type_fqn, member), ...] for member references
    against a confidently-inferred type that aren't in that type's known
    surface. `surface` maps FQN ("GTA.Ped") -> list of valid member names."""
    if not surface:
        return []
    usable = {k: v for k, v in surface.items()
              if isinstance(v, list) and len(v) >= _MIN_MEMBER_SURFACE}
    if not usable:
        return []

    out: list[tuple[int, str, str]] = []
    seen_keys: set[tuple[str, str]] = set()

    def _check(pos: int, type_fqn: str, member: str) -> None:
        if member in _MEMBER_ALWAYS_OK:
            return
        valid = usable.get(type_fqn)
        if not valid or member in valid:
            return
        key = (type_fqn, member)
        if key in seen_keys:
            return
        seen_keys.add(key)
        out.append((pos, type_fqn, member))

    var_types = _infer_member_var_types(source)
    if var_types:
        var_member_re = re.compile(
            r'\b(' + "|".join(re.escape(v) for v in var_types) + r')\.([A-Za-z_]\w*)\b')
        for m in var_member_re.finditer(source):
            _check(m.start(2), var_types[m.group(1)], m.group(2))

    for pat, type_fqn in _MEMBER_CHAIN_PATTERNS:
        for m in pat.finditer(source):
            _check(m.start(1), type_fqn, m.group(1))

    for pat, type_fqn in _MEMBER_STATIC_PATTERNS:
        for m in pat.finditer(source):
            _check(m.start(1), type_fqn, m.group(1))

    out.sort(key=lambda t: t[0])
    return out


def lint_member_references(text: str, *, surface: "dict | None") -> str:
    """Neutralize any line referencing an invented member on a confidently-
    inferred SHVDN3 type (e.g. Ped.GetNearestVehicle, which doesn't exist).
    Same conservative strategy as lint_hash_references: comment out the whole
    line, skip (leave alone) a line whose own brace count isn't locally
    balanced, never guess a replacement. `surface=None`/empty is a no-op.
    """
    if not text or not surface:
        return text
    bad = find_invalid_member_refs(text, surface=surface)
    if not bad:
        return text

    bad_lines: dict[int, list[str]] = {}
    for pos, type_fqn, member in bad:
        line_no = text.count("\n", 0, pos)
        bad_lines.setdefault(line_no, []).append(f"{type_fqn.split('.')[-1]}.{member}")

    out: list[str] = []
    flagged: set[str] = set()
    skipped: set[str] = set()
    for i, line in enumerate(text.splitlines()):
        if line.lstrip().startswith("//"):
            out.append(line)
            continue
        names = bad_lines.get(i)
        if not names:
            out.append(line)
            continue
        if not _safe_to_comment_out(line):
            disabled = _disable_bad_header(line, names)
            if disabled is not None:
                flagged.update(names)
                out.append(disabled)
                continue
            skipped.update(names)
            out.append(line)
            continue
        flagged.update(names)
        indent = line[: len(line) - len(line.lstrip())]
        out.append(f"{indent}// AUDIT: removed invalid member reference(s) "
                   f"{', '.join(sorted(set(names)))} -- {line.strip()}")
    if flagged:
        console.print(f"  [yellow]FIX[/]  removed statement(s) referencing "
                      f"invalid member(s): {', '.join(sorted(flagged))}")
    if skipped:
        console.print(f"  [yellow]WARN[/] could not safely remove invalid member "
                      f"reference(s) on a control-flow or brace-mixed line: "
                      f"{', '.join(sorted(skipped))}")
    return "\n".join(out)


# ── Enum member reference checker ─────────────────────────────────────────────
# LLMs also invent plausible-but-wrong ENUM values (Weather.Rain -- the real
# member is Weather.Raining), not just methods and natives. Corpus-mined:
# 133 occurrences of exactly this shape across a 1191-file independent
# dataset, the 4th most common real failure after Hash natives, invented
# members, and an optional-DLL dependency issue that doesn't apply here.
# Unlike member-on-type checking, no variable/chain type inference is
# needed: in `EnumType.Member`, EnumType names the type directly and
# unambiguously wherever it appears (Weather.X always means the Weather
# enum), so this checks every occurrence of a known enum name, full stop.
_MIN_ENUM_SURFACE = 2


def find_invalid_enum_refs(source: str, *, enums: "dict | None") -> list[tuple[int, str, str]]:
    """Return [(char_offset, enum_name, member), ...] for EnumType.Member
    references where EnumType is a known SHVDN3 enum (`enums` maps enum
    name -> list of valid member names) but Member isn't a real value."""
    if not enums:
        return []
    usable = {k: v for k, v in enums.items()
              if isinstance(v, list) and len(v) >= _MIN_ENUM_SURFACE}
    if not usable:
        return []
    pattern = re.compile(
        r'\b(' + "|".join(re.escape(k) for k in usable) + r')\.([A-Za-z_]\w*)\b')

    out: list[tuple[int, str, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    for m in pattern.finditer(source):
        enum_name, member = m.group(1), m.group(2)
        if member in _MEMBER_ALWAYS_OK or member in usable[enum_name]:
            continue
        key = (enum_name, member)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out.append((m.start(2), enum_name, member))

    out.sort(key=lambda t: t[0])
    return out


def lint_enum_references(text: str, *, enums: "dict | None") -> str:
    """Neutralize any line referencing an invented enum value (e.g.
    Weather.Rain, which doesn't exist -- the real member is Raining). Same
    conservative strategy as lint_hash_references/lint_member_references:
    comment out a complete statement, swap a bare if/while header's
    condition for `false`, or leave the line alone entirely if neither is
    safe. `enums=None`/empty is a no-op.
    """
    if not text or not enums:
        return text
    bad = find_invalid_enum_refs(text, enums=enums)
    if not bad:
        return text

    bad_lines: dict[int, list[str]] = {}
    for pos, enum_name, member in bad:
        line_no = text.count("\n", 0, pos)
        bad_lines.setdefault(line_no, []).append(f"{enum_name}.{member}")

    out: list[str] = []
    flagged: set[str] = set()
    skipped: set[str] = set()
    for i, line in enumerate(text.splitlines()):
        if line.lstrip().startswith("//"):
            out.append(line)
            continue
        names = bad_lines.get(i)
        if not names:
            out.append(line)
            continue
        if not _safe_to_comment_out(line):
            disabled = _disable_bad_header(line, names)
            if disabled is not None:
                flagged.update(names)
                out.append(disabled)
                continue
            skipped.update(names)
            out.append(line)
            continue
        flagged.update(names)
        indent = line[: len(line) - len(line.lstrip())]
        out.append(f"{indent}// AUDIT: removed invalid enum reference(s) "
                   f"{', '.join(sorted(set(names)))} -- {line.strip()}")
    if flagged:
        console.print(f"  [yellow]FIX[/]  removed statement(s) referencing "
                      f"invalid enum member(s): {', '.join(sorted(flagged))}")
    if skipped:
        console.print(f"  [yellow]WARN[/] could not safely remove invalid enum "
                      f"member(s) on a control-flow or brace-mixed line: "
                      f"{', '.join(sorted(skipped))}")
    return "\n".join(out)


# CS1501 — Vector3.Distance(v)/.Dot(v) called as a single-arg INSTANCE method.
# Both are static-only two-argument methods on Vector3; no SHVDN3 type has an
# instance .Distance(Vector3)/.Dot(Vector3) overload, so this pattern is always
# wrong regardless of the receiver's type (mirrors the reference audit tool's
# rule exactly — same detection regex, receiver-blind by design).
_VEC_STATIC_MISUSE_RE = re.compile(r'(?<!\bVector3)\b\w+\.(?:Distance|Dot)\s*\(\s*\w+\s*\)')


def lint_vector3_static_misuse(text: str) -> str:
    """Neutralize `receiver.Distance(x)` / `receiver.Dot(x)` — CS1501, since
    both are static-only on Vector3. No safe mechanical rewrite is attempted:
    the receiver may be a multi-segment chain (`a.b.Position.Distance(x)`),
    and this checker only sees the last identifier before the dot, so a
    rewrite could reconstruct the wrong expression. Same comment-out strategy
    as the other lint_* passes here."""
    if not text or not _VEC_STATIC_MISUSE_RE.search(text):
        return text
    out: list[str] = []
    flagged = False
    skipped = False
    for line in text.splitlines():
        if line.lstrip().startswith("//"):
            out.append(line)
            continue
        if not _VEC_STATIC_MISUSE_RE.search(line):
            out.append(line)
            continue
        if not _safe_to_comment_out(line):
            disabled = _disable_bad_header(line, ["Vector3.Distance/.Dot misuse"])
            if disabled is not None:
                flagged = True
                out.append(disabled)
                continue
            skipped = True
            out.append(line)
            continue
        flagged = True
        indent = line[: len(line) - len(line.lstrip())]
        out.append(f"{indent}// AUDIT: removed Vector3.Distance/.Dot instance-call "
                   f"misuse (CS1501) -- {line.strip()}")
    if flagged:
        console.print("  [yellow]FIX[/]  removed statement(s) misusing "
                      "Vector3.Distance/.Dot as an instance method")
    if skipped:
        console.print("  [yellow]WARN[/] could not safely remove a Vector3.Distance/.Dot "
                      "misuse on a control-flow or brace-mixed line")
    return "\n".join(out)


# CS1061/CS0117 — bare `Task.X(...)` static calls. TaskInvoker ("Task") only
# exists as an instance member (`ped.Task`), never as a static class — no
# SHVDN3 type has any static Task.* method. Corpus-mined: 43 occurrences
# across a 1191-file independent dataset, right behind Hash/member/enum
# invention. Pure blocklist, no type inference needed — mirrors the
# reference audit tool's own broader rule (its narrower sibling only
# flags a curated list of ~25 known method names; this catches ANY bare
# `Task.` reference regardless of what follows, the same way `ped.Task.X`
# vs. bare `Task.X` is unambiguous without needing to know X at all).
_BARE_TASK_RE = re.compile(r'(?<!\.)\bTask\s*\.')


def lint_bare_task_static_misuse(text: str) -> str:
    """Neutralize `Task.X(...)` used as if Task were a static class — it only
    exists as `ped.Task`. Same comment-out-or-disable-header strategy as the
    other lint_* passes here."""
    if not text or not _BARE_TASK_RE.search(text):
        return text
    out: list[str] = []
    flagged = False
    skipped = False
    for line in text.splitlines():
        if line.lstrip().startswith("//"):
            out.append(line)
            continue
        if not _BARE_TASK_RE.search(line):
            out.append(line)
            continue
        if not _safe_to_comment_out(line):
            disabled = _disable_bad_header(line, ["bare Task.X() static misuse"])
            if disabled is not None:
                flagged = True
                out.append(disabled)
                continue
            skipped = True
            out.append(line)
            continue
        flagged = True
        indent = line[: len(line) - len(line.lstrip())]
        out.append(f"{indent}// AUDIT: removed bare Task.X() static misuse "
                   f"(Task is instance-only via ped.Task) -- {line.strip()}")
    if flagged:
        console.print("  [yellow]FIX[/]  removed statement(s) misusing "
                      "Task as a static class")
    if skipped:
        console.print("  [yellow]WARN[/] could not safely remove a bare Task.X() "
                      "misuse on a control-flow or brace-mixed line")
    return "\n".join(out)


def clamp_enum(v: str, allowed: tuple[str, ...], default: str) -> str:
    """Map an LLM value onto an allowed literal, tolerant of case and of models
    that echo the whole option menu (e.g. "uGUI | UIToolkit"). Falls back to
    `default` when nothing matches.

    Use this in a Pydantic ``@field_validator(..., mode="before")`` for any
    Literal field so a loose model can never crash schema validation — the exact
    failure mode deepseek-coder hit on the HUD generator."""
    s = str(v).strip()
    lower = {a.lower(): a for a in allowed}
    # Exact (case-insensitive) match first.
    if s.lower() in lower:
        return lower[s.lower()]
    # Pick the first allowed value mentioned anywhere in the string.
    for token in re.split(r"[^A-Za-z0-9_]+", s):
        if token.lower() in lower:
            return lower[token.lower()]
    return default


def lint_fields(fields: list[CodeField], *, reserved: set[str]) -> list[CodeField]:
    """Drop fields whose name collides with template-emitted names or duplicates."""
    out: list[CodeField] = []
    seen: set[str] = set()
    for f in fields:
        key = f.name.lower()
        if key in reserved or key in seen:
            console.print(f"  [yellow]FIX[/]  dropped duplicate/reserved field {f.name!r}")
            continue
        seen.add(key)
        out.append(f)
    return out


# ── Reusable mixin ────────────────────────────────────────────────────────────

class CodeExtensibleSchema(BaseModel):
    """
    Mixin base for any schema that wants LLM-authored C# fragments.

    The LLM fills these from the concept; the pipeline lints them (see
    `lint_extensible`) so they're safe; the template renders them through the
    shared injection macro (`templates/unity/micro/_inject.j2`). Because the
    fragments live IN the schema, Step 6 stays a pure function of the schema —
    determinism is preserved while the LLM gains real structural influence.

    `*_extra` map to lifecycle methods on the generated MonoBehaviour:
      awake_extra        -> end of Awake()
      update_extra       -> end of the per-frame method (Update / LateUpdate)
      fixed_update_extra -> a FixedUpdate() body (emitted only when non-empty)
    """
    extra_usings:       list[str]        = Field(default_factory=list)
    extra_fields:       list[CodeField]  = Field(default_factory=list)
    awake_extra:        str              = ""
    update_extra:       str              = ""
    fixed_update_extra: str              = ""
    extra_methods:      list[CodeMethod] = Field(default_factory=list)


def lint_extensible(schema: CodeExtensibleSchema, *,
                    reserved_fields: set[str],
                    template_usings: set[str]) -> CodeExtensibleSchema:
    """
    Run the full defensive lint over every `extra_*` field of an extensible
    schema, in place. Mirrors the controller's hand-written pass so every
    generator gets identical, battle-tested cleaning for free.
    """
    schema.extra_usings = lint_usings(schema.extra_usings, already_in_template=template_usings,
                                      allowed=_KNOWN_NAMESPACES)
    schema.extra_fields = lint_fields(schema.extra_fields, reserved=reserved_fields)
    schema.awake_extra        = lint_code_fragment(schema.awake_extra,        what="Awake() body")
    schema.update_extra       = lint_code_fragment(schema.update_extra,       what="per-frame body")
    schema.fixed_update_extra = lint_code_fragment(schema.fixed_update_extra, what="FixedUpdate() body")
    taken: set[str] = set()
    cleaned: list[CodeMethod] = []
    for m in schema.extra_methods:
        m2 = lint_method(m, taken_names=taken)
        if m2 is not None:
            cleaned.append(m2)
    schema.extra_methods = cleaned

    # Auto-heal missing usings: if the authored content needs a namespace the
    # model forgot (e.g. List<> without System.Collections.Generic), add it.
    blob = "\n".join(
        [schema.awake_extra, schema.update_extra, schema.fixed_update_extra]
        + [f"{f.type} {f.default or ''}" for f in schema.extra_fields]
        + [f"{m.signature}\n{m.body}" for m in schema.extra_methods]
    )
    for pattern, ns in _USING_TRIGGERS:
        if (ns not in schema.extra_usings and ns not in template_usings
                and re.search(pattern, blob)):
            schema.extra_usings.append(ns)
            console.print(f"  [magenta]FIX[/]  auto-added missing using {ns!r}")
    return schema


# ── Shared prompt instructions ────────────────────────────────────────────────
# Appended to a generator's Step-3 schema-fill prompt so the LLM knows to author
# prompt-specific behavior in the extras instead of leaving them empty.

UNITY_EXTRA_INSTRUCTIONS = """\

────────── PROMPT-SPECIFIC BEHAVIOR (this is how the prompt reaches the file) ──────────
The template already emits the BASELINE for the chosen variant — you do NOT
re-write it. You author the details unique to THIS prompt using the extra_* fields:

  extra_usings:       extra namespaces, e.g. "UnityEngine.Events" (no "using", no ";")
  extra_fields:       extra [SerializeField] declarations the behavior needs
                      each: {{"name": "...", "type": "float", "default": "0f", "header": "...", "tooltip": "..."}}
  awake_extra:        C# statements appended inside Awake()
  update_extra:       C# statements appended inside the per-frame method
  fixed_update_extra: C# statements for a FixedUpdate() (omit if not needed)
  extra_methods:      helper methods, each {{"signature": "private void Foo(int x)", "body": "..."}}

C# RULES:
- Pure C#, never Python. No "using" lines inside bodies — put them in extra_usings.
- No braces around extra_methods bodies — the template adds them.
- Reference your own extra_fields by name; they become private members.
- If the prompt has no special behavior, leave every extra_* empty.

Add these keys to your JSON output (alongside the structural keys):
  "extra_usings": [], "extra_fields": [], "awake_extra": "",
  "update_extra": "", "fixed_update_extra": "", "extra_methods": []
"""
