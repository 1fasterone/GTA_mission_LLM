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
from typing import Optional, Protocol

from pydantic import BaseModel, Field, field_validator, model_validator
from rich.console import Console

console = Console()


# ── Schema primitives ─────────────────────────────────────────────────────────

# C# identifier — must start with a letter or _, followed by letters/digits/_.
_CSHARP_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Allowed `type` shapes: bare identifiers (float, MyType), generics (List<int>),
# arrays (int[]), and dotted refs (UnityEngine.Vector3). Defensive whitelist.
_CSHARP_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.<>,\[\]?\s]*$")


def escape_cs_string(text: str) -> str:
    """Escape LLM prose for a C# regular string literal.

    `header`/`tooltip` are interpolated raw into `[Header("…")]` /
    `[Tooltip("…")]` by every template that renders extra_fields
    (templates/unity/micro/_inject.j2 and the seven generators with their own
    inline copy), so a quote in the model's prose terminates the literal early
    and the rest of the line parses as garbage. Hit live on a DropZone run:

        [Tooltip("Current active quest identifier (e.g., "RocBiomeFreefall", ...")]

    → CS1003 at column 75 in both NpcDialogPlayer.cs and NpcDialogTrigger.cs.
    Escaping here rather than in the templates fixes all eight injection sites
    at once, and keeps the fix on the data that is actually untrusted.

    Backslash first — otherwise the pass would double-escape the backslashes
    this function itself introduces. Newlines become `\\n` rather than being
    stripped: a literal newline is equally fatal inside the literal, and the
    escape preserves the author's text.
    """
    out = text.replace("\\", "\\\\").replace('"', '\\"')
    for raw, esc in (("\r\n", "\\n"), ("\r", "\\n"), ("\n", "\\n"), ("\t", "\\t")):
        out = out.replace(raw, esc)
    return out


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

    @field_validator("header", "tooltip", mode="after")
    @classmethod
    def _escape_for_literal(cls, v):
        """Only header/tooltip — `default` is NOT inside a literal, it IS the
        literal (`default='\"\"'` renders `= \"\"`), so escaping it would emit
        `= \\\"\\\"` and break the declaration."""
        return None if v is None else escape_cs_string(v)


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


def _looks_like_prose(text: str) -> bool:
    """True when a fragment is an English sentence describing behavior
    instead of C# statements — the LLM writes "Apply suspension forces
    based on terrain displacement..." straight into `update_extra` instead
    of code (hit live: DirtPhysicsController.cs / RaceLogicManager.cs,
    29 repeated CS1002/CS1003 errors — the prose landed verbatim inside
    Update()/FixedUpdate()). Every valid C# statement or statement list
    ends at least one statement in `;` or a block in `{`/`}` — a fragment
    with NONE of those three characters anywhere cannot possibly compile
    as statements, so this can never reject genuine code, only prose."""
    return not any(c in text for c in (";", "{", "}"))


def _code_mask(text: str) -> bytearray:
    """Per-character mask: 1 where the character is real CODE, 0 inside a
    comment or a char/string literal.

    The single source of truth for "is this position code?", shared by the
    brace counter and by every lint pass that rewrites source with a regex.
    Both classes of bug it prevents are the same one — a pattern over
    untokenized C# cannot tell a `}` in a comment from a real closer, or the
    word `out` in an English sentence from a real out-parameter:

        if (_isDirty) { int loc = _idx; // Validate state before save }
            -> the old brace COUNT saw balance; Update() never closed (CS0106)
        Debug.Log("Good luck out there.");
            -> the out-param regex rewrote prose to "Good luck out var there."

    Handles `//` and `/* */` comments, char literals, regular strings (with
    `\\` escapes), verbatim strings (`@"…"`, where `""` is an escaped quote)
    and interpolated strings (`$"…"`, either prefix order). Interpolated
    strings are masked WHOLE: an interpolation hole's braces balance within
    the literal, so ignoring them keeps the brace count right without having
    to parse the hole.
    """
    n = len(text)
    mask = bytearray(b"\x01" * n)
    i = 0
    while i < n:
        ch = text[i]

        if ch == "/" and i + 1 < n:
            if text[i + 1] == "/":                    # line comment
                j = text.find("\n", i)
                j = n if j == -1 else j
                mask[i:j] = b"\x00" * (j - i)
                i = j
                continue
            if text[i + 1] == "*":                    # block comment
                j = text.find("*/", i + 2)
                j = n if j == -1 else j + 2
                mask[i:j] = b"\x00" * (j - i)
                i = j
                continue

        if ch == "'":                                 # char literal: '{', '\''
            start = i + 1
            while start < n and text[start] != "'":
                start += 2 if text[start] == "\\" else 1
            start = min(start + 1, n)
            mask[i:start] = b"\x00" * (start - i)
            i = start
            continue

        if ch in "@$\"":                              # string literal
            start, j, verbatim = i, i, False
            while j < n and text[j] in "@$":          # $@"…" and @$"…" both
                verbatim = verbatim or text[j] == "@"
                j += 1
            if j >= n or text[j] != '"':
                # A bare `@` (verbatim identifier such as `@class`) or a stray
                # `$` — not a literal prefix, so it stays code.
                i = start + 1
                continue
            j += 1
            if verbatim:
                while j < n:
                    if text[j] == '"':
                        if j + 1 < n and text[j + 1] == '"':
                            j += 2                    # "" — escaped quote
                            continue
                        j += 1
                        break
                    j += 1
            else:
                while j < n and text[j] != '"':
                    if text[j] == "\\":
                        j += 2
                    elif text[j] == "\n":
                        break                         # unterminated literal
                    else:
                        j += 1
                if j < n and text[j] == '"':
                    j += 1
            j = min(j, n)
            mask[i:j] = b"\x00" * (j - i)
            i = j
            continue

        i += 1
    return mask


def _code_brace_depth(text: str) -> int:
    """Net brace depth counting only braces that are real CODE delimiters.

    A lexical pass (`_code_mask`), not a regex — the two failure modes this
    exists for (a `}` inside a comment, a `{` inside a string) are exactly
    what a pattern over untokenized C# gets wrong.
    """
    mask = _code_mask(text)
    depth = 0
    for i, ch in enumerate(text):
        if not mask[i]:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    return depth


def _balance_braces(text: str) -> str:
    """Auto-close unbalanced { } with appended close-braces. Never reduces.

    Counts CODE braces only (`_code_brace_depth`) — a brace inside a comment
    or a string literal is not a delimiter. Hit live: an `update_extra`
    fragment arrived from the model as a single line,

        if (_isDirty) { int loc = _idx; // Validate state before save }

    where the closing brace sits inside the trailing `//` comment. Brace
    CHARACTERS balance, so the old `text.count()` saw nothing to do and the
    fragment shipped with `Update()` never closed; every following member then
    parsed as a local function and the first `public` one raised CS0106
    (SaveSystem.cs(77,5), live). A lexical count sees depth 1 and appends the
    real closer on its own line, after the comment that swallowed the first.

    Still never reduces: a fragment with MORE closers than openers (which eats
    the enclosing method the same way) is left alone rather than guessed at —
    dropping a brace can silently merge two blocks. The syntax gate catches it
    (src/csharp_syntax_gate.py).
    """
    depth = _code_brace_depth(text)
    if depth > 0:
        text = text + ("\n" + "}" * depth)
        console.print(f"  [magenta]FIX[/]  closed {depth} unterminated block(s) "
                      f"— brace in a comment/string is not a delimiter")
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
    # CODE positions only. `out` is an ordinary English word, and this pattern
    # used to fire on it wherever it appeared — hit live, a dialogue line
    # `Debug.Log("Good luck out there.");` was rewritten to
    # `"Good luck out var there."`, corrupting shipped prose. Same defect class
    # as the brace counter above: a regex cannot see literal boundaries.
    mask = _code_mask(text)
    seen: set[str] = set()
    changed = False

    def _declared_in_code(ident: str) -> bool:
        """Already declared explicitly in this fragment (`var hit` /
        `RaycastHit hit`)? Match `var`, a primitive, or a Capitalized type name
        before the identifier — deliberately NOT the `out`/`ref` keyword itself
        (lowercase, non-primitive), which would otherwise look like a
        declaration. Masked too: a declaration named only inside a comment
        (`// RaycastHit hit;`) would otherwise suppress a rewrite that is
        genuinely needed, trading this bug for a CS0103.
        """
        pattern = re.compile(
            rf'\b(?:var|int|float|double|bool|string|byte|long|short|'
            rf'char|object|[A-Z][\w<>\[\].]*)\s+{re.escape(ident)}\b')
        return any(mask[d.start()] for d in pattern.finditer(text))

    def repl(m: "re.Match") -> str:
        nonlocal changed
        if not mask[m.start()]:
            return m.group(0)                       # inside a comment/literal
        ident = m.group(1)
        if ident in seen:
            return m.group(0)                       # reuse the first declaration
        if _declared_in_code(ident):
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
    if _looks_like_prose(text):
        console.print(f"  [yellow]FIX[/]  rejected prose (not C#) {what}: {text[:60]!r}")
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
                allowed: set[str] | frozenset[str] | None = None) -> list[str]:
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


# CS1502/CS1503 — `ref x` passed to Function.Call. SHVDN3's Function.Call
# takes InputArgument[] params; `ref float`/`ref int`/etc. isn't implicitly
# convertible to InputArgument, so a model reaching for .NET's normal
# out-param idiom on a native call fails to compile. The correct shape
# routes through OutputArgument + GetResult<T>(), which needs three lines
# and knowledge of the target type — too much to safely synthesize here, so
# (like the other lint_* passes in this file) this only neutralizes the bad
# line rather than attempting the OutputArgument rewrite.
_FUNCTION_CALL_REF_RE = re.compile(
    r'\bFunction\.Call\s*(?:<[^>]+>)?\s*\([^;]*?\bref\s+\w+\b[^;]*?\)\s*;')


def lint_function_call_ref_misuse(text: str) -> str:
    """Neutralize `Function.Call(..., ref x, ...)` — InputArgument has no
    implicit conversion from a `ref` parameter (CS1502/CS1503). Same
    comment-out-or-disable-header strategy as the other lint_* passes here."""
    if not text or not _FUNCTION_CALL_REF_RE.search(text):
        return text
    out: list[str] = []
    flagged = False
    skipped = False
    for line in text.splitlines():
        if line.lstrip().startswith("//"):
            out.append(line)
            continue
        if not _FUNCTION_CALL_REF_RE.search(line):
            out.append(line)
            continue
        if not _safe_to_comment_out(line):
            disabled = _disable_bad_header(line, ["Function.Call(ref ...) misuse"])
            if disabled is not None:
                flagged = True
                out.append(disabled)
                continue
            skipped = True
            out.append(line)
            continue
        flagged = True
        indent = line[: len(line) - len(line.lstrip())]
        out.append(f"{indent}// AUDIT: removed Function.Call(ref ...) misuse "
                   f"(InputArgument has no ref conversion, use OutputArgument) "
                   f"-- {line.strip()}")
    if flagged:
        console.print("  [yellow]FIX[/]  removed statement(s) passing `ref` "
                      "into Function.Call")
    if skipped:
        console.print("  [yellow]WARN[/] could not safely remove a Function.Call(ref ...) "
                      "misuse on a control-flow or brace-mixed line")
    return "\n".join(out)


# CS1061/CS0117 — World's array/direct-return lookups (GetNearbyPeds,
# GetAllVehicles, ...) hallucinated with a C#-idiomatic `out` parameter that
# doesn't exist on these particular overloads; they just return the array
# directly. Restricted to methods confirmed to have NO out-parameter overload
# at all (some World.GetX methods legitimately use `out`, e.g.
# GetGroundHeight/GetSafePositionForPed/GetStreetName — deliberately excluded).
_WORLD_OUT_PARAM_RE = re.compile(
    r'\bWorld\.(?:GetNearbyPeds|GetNearbyVehicles|GetAllPeds|GetAllVehicles|'
    r'GetAllProps|GetAllEntities|GetClosestPed|GetClosestVehicle|'
    r'GetAllProjectiles|GetAllPickupObjects)\s*\([^;]*?\bout\s+\w+\b[^;]*?\)')


def lint_world_get_out_param_misuse(text: str) -> str:
    """Neutralize a hallucinated `out` parameter on a World.GetX call that
    just returns its result directly (CS1061/CS0117). Same
    comment-out-or-disable-header strategy as the other lint_* passes here."""
    if not text or not _WORLD_OUT_PARAM_RE.search(text):
        return text
    out: list[str] = []
    flagged = False
    skipped = False
    for line in text.splitlines():
        if line.lstrip().startswith("//"):
            out.append(line)
            continue
        if not _WORLD_OUT_PARAM_RE.search(line):
            out.append(line)
            continue
        if not _safe_to_comment_out(line):
            disabled = _disable_bad_header(line, ["World.GetX(out ...) misuse"])
            if disabled is not None:
                flagged = True
                out.append(disabled)
                continue
            skipped = True
            out.append(line)
            continue
        flagged = True
        indent = line[: len(line) - len(line.lstrip())]
        out.append(f"{indent}// AUDIT: removed hallucinated `out` parameter on "
                   f"World.GetX call (it returns its result directly) "
                   f"-- {line.strip()}")
    if flagged:
        console.print("  [yellow]FIX[/]  removed statement(s) passing a "
                      "hallucinated `out` parameter to a World.GetX call")
    if skipped:
        console.print("  [yellow]WARN[/] could not safely remove a World.GetX(out ...) "
                      "misuse on a control-flow or brace-mixed line")
    return "\n".join(out)


# CS1503 — a whole Vector3 value passed straight into Function.Call.
# InputArgument has no Vector3 conversion, so a model that hasn't decomposed
# a position into X/Y/Z floats (or reached for the World.CreateX wrapper
# instead of the raw native) fails to compile. Needs real type inference —
# a receiver-blind regex would flag every non-Vector3 argument too — so this
# tracks locally-declared Vector3 variables plus the handful of confirmed
# Vector3-typed properties (Position/Rotation/*Vector) used as a whole
# argument rather than decomposed.
_VECTOR3_VAR_DECL_RE = re.compile(r'\bVector3\s+([A-Za-z_]\w*)\b(?=\s*[=,;)\]])')
_VECTOR3_PROP_ARG_RE = re.compile(
    r'^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\.(?:Position|Rotation|ForwardVector|UpVector|RightVector)$')
_FUNCTION_CALL_ARGS_RE = re.compile(r'Function\.Call\s*(?:<[^>]+>)?\s*\(([^;]*?)\)\s*;')


def _split_top_level_args(args_text: str) -> list[str]:
    """Split a Function.Call argument list on top-level commas only — commas
    inside a nested call/indexer (`new Vector3(x, y, z)`) don't separate
    arguments."""
    depth = 0
    parts: list[str] = []
    current: list[str] = []
    for ch in args_text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def find_vector3_raw_call_args(source: str) -> list[int]:
    """Return the sorted 0-based line numbers of Function.Call(...) statements
    that pass a locally-declared Vector3 variable, or a Vector3-typed
    property chain, directly as an argument instead of decomposed X/Y/Z."""
    if not source:
        return []
    declared = {m.group(1) for m in _VECTOR3_VAR_DECL_RE.finditer(source)}
    bad_lines: set[int] = set()
    for m in _FUNCTION_CALL_ARGS_RE.finditer(source):
        for part in _split_top_level_args(m.group(1)):
            p = part.strip()
            if not p:
                continue
            if p in declared or _VECTOR3_PROP_ARG_RE.match(p):
                bad_lines.add(source.count("\n", 0, m.start()))
                break
    return sorted(bad_lines)


def lint_vector3_raw_call_arg(text: str) -> str:
    """Neutralize a Function.Call(...) statement that passes a whole Vector3
    value directly (CS1503 — InputArgument has no Vector3 conversion). Same
    comment-out-or-disable-header strategy as the other lint_* passes here."""
    if not text:
        return text
    bad_lines = set(find_vector3_raw_call_args(text))
    if not bad_lines:
        return text
    out: list[str] = []
    flagged = False
    skipped = False
    for i, line in enumerate(text.splitlines()):
        if line.lstrip().startswith("//"):
            out.append(line)
            continue
        if i not in bad_lines:
            out.append(line)
            continue
        if not _safe_to_comment_out(line):
            disabled = _disable_bad_header(line, ["Vector3-as-raw-Function.Call-arg misuse"])
            if disabled is not None:
                flagged = True
                out.append(disabled)
                continue
            skipped = True
            out.append(line)
            continue
        flagged = True
        indent = line[: len(line) - len(line.lstrip())]
        out.append(f"{indent}// AUDIT: removed Function.Call passing a raw Vector3 "
                   f"argument (decompose into .X/.Y/.Z, InputArgument has no Vector3 "
                   f"conversion) -- {line.strip()}")
    if flagged:
        console.print("  [yellow]FIX[/]  removed statement(s) passing a raw Vector3 "
                      "value into Function.Call")
    if skipped:
        console.print("  [yellow]WARN[/] could not safely remove a Vector3-as-raw-arg "
                      "misuse on a control-flow or brace-mixed line")
    return "\n".join(out)


# CS1061 — `.Position`/`.Rotation`/etc. accessed on a value confidently known
# to be a plain GTA.Math.Vector3 (a bare X/Y/Z struct), not an Entity. Live
# regression: an LLM-authored extra_init wrote `SpawnOrigin.Position + new
# Vector3(5f, 0f, 0f)` — SpawnOrigin is the mission template's own
# `private static readonly Vector3 SpawnOrigin` constant (see mission.cs.j2),
# always a raw Vector3, never an Entity — Vector3 has no `.Position` member.
# Almost certainly an overgeneralization of the few-shot idiom
# `Game.Player.Character.Position + new Vector3(...)` onto a receiver that's
# already a Vector3. Same conservative-inference approach as
# find_invalid_member_refs: only fires on a local `Vector3 x = ...;`
# declaration or the reserved template constant name, never on an unknown
# identifier.
_VECTOR3_RESERVED_IDENTS = {"SpawnOrigin"}
_VECTOR3_ENTITY_ONLY_PROPS = {
    "Position", "Rotation", "Heading", "ForwardVector", "UpVector",
    "RightVector", "Quaternion", "RelativePosition",
}


def find_vector3_entity_prop_misuse(source: str) -> list[tuple[int, str, str]]:
    """Return [(char_offset, var_name, prop), ...] for Entity-only property
    access (`.Position`, `.Rotation`, ...) on an identifier confidently known
    to be a `GTA.Math.Vector3` — either a local `Vector3 x = ...;`
    declaration or the reserved template-declared `SpawnOrigin` constant.
    Vector3 has no such properties; they only exist on Entity-derived types
    (Ped/Vehicle/Prop/Camera)."""
    declared = {m.group(1) for m in _VECTOR3_VAR_DECL_RE.finditer(source)}
    idents = declared | _VECTOR3_RESERVED_IDENTS
    pattern = re.compile(
        r'\b(' + "|".join(re.escape(n) for n in idents) + r')\.([A-Za-z_]\w*)\b')
    out: list[tuple[int, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for m in pattern.finditer(source):
        var, member = m.group(1), m.group(2)
        if member not in _VECTOR3_ENTITY_ONLY_PROPS:
            continue
        key = (var, member)
        if key in seen:
            continue
        seen.add(key)
        out.append((m.start(2), var, member))
    out.sort(key=lambda t: t[0])
    return out


def lint_vector3_entity_prop_misuse(text: str) -> str:
    """Neutralize `<Vector3>.Position` / `.Rotation` / etc. — these are
    Entity-only properties that don't exist on a plain Vector3 (CS1061).
    Same comment-out-or-disable-header strategy as the other lint_* passes
    here."""
    if not text:
        return text
    bad = find_vector3_entity_prop_misuse(text)
    if not bad:
        return text

    bad_lines: dict[int, list[str]] = {}
    for pos, var, member in bad:
        line_no = text.count("\n", 0, pos)
        bad_lines.setdefault(line_no, []).append(f"{var}.{member}")

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
        out.append(f"{indent}// AUDIT: removed Entity-only property access on a "
                   f"Vector3 {', '.join(sorted(set(names)))} (Vector3 has no such "
                   f"member) -- {line.strip()}")
    if flagged:
        console.print(f"  [yellow]FIX[/]  removed statement(s) accessing an "
                      f"Entity-only property on a Vector3: "
                      f"{', '.join(sorted(flagged))}")
    if skipped:
        console.print("  [yellow]WARN[/] could not safely remove a Vector3 "
                      "Entity-only-property misuse on a control-flow or "
                      "brace-mixed line")
    return "\n".join(out)


# CS0103 — `catch (Exception) { ... ex ... }`. Writing `catch (Exception)`
# without a variable name leaves `ex` out of scope, so a reference to it
# inside the block fails to compile. Unlike the other lint_* passes here,
# this has a fully safe mechanical fix (rather than a neutralize-the-line
# fallback): declare the variable the block already expects, since doing so
# can only ever make previously-uncompilable code compile, never change
# behavior for code that already worked.
def _find_matching_brace(text: str, open_idx: int) -> int:
    """Return the index of the `}` matching the `{` at `open_idx`, or -1 if
    the braces from that point on are unbalanced."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


_UNDECLARED_CATCH_RE = re.compile(r'catch\s*\(\s*Exception\s*\)\s*\{')
_BARE_EX_RE = re.compile(r'\bex\b')


def lint_undeclared_catch_exception_var(text: str) -> str:
    """Rewrite `catch (Exception) { ... ex ... }` to `catch (Exception ex) { ... }`
    whenever the block references a bare `ex` that would otherwise be out of
    scope (CS0103). A block that never uses the caught exception is left
    alone — `catch (Exception)` with no reference to `ex` already compiles."""
    if not text or not _UNDECLARED_CATCH_RE.search(text):
        return text
    out: list[str] = []
    pos = 0
    fixed = 0
    for m in _UNDECLARED_CATCH_RE.finditer(text):
        if m.start() < pos:
            continue  # inside an already-rewritten block, skip
        brace_idx = m.end() - 1
        close_idx = _find_matching_brace(text, brace_idx)
        if close_idx == -1:
            continue  # unbalanced braces -- leave for the compiler to report
        body = text[brace_idx + 1:close_idx]
        if not _BARE_EX_RE.search(body):
            continue
        out.append(text[pos:m.start()])
        out.append("catch (Exception ex) {")
        pos = m.end()
        fixed += 1
    out.append(text[pos:])
    if not fixed:
        return text
    console.print("  [yellow]FIX[/]  declared the missing exception variable in "
                  f"{fixed} `catch (Exception)` block(s) referencing bare `ex`")
    return "".join(out)


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


# ── Field initializer sanitizing ──────────────────────────────────────────────
# `default` is the one injected value that is not INSIDE a literal — it IS the
# initializer expression, spliced raw into `private {type} {name} = {default};`
# by all thirteen render sites (templates/unity/micro/_inject.j2, the seven
# generators carrying an inline copy of it, SaveData.cs.j2, CustomBehaviour.cs.j2,
# templates/gta5/micro/_inject.j2, mission.cs.j2, minigame.cs.j2). That is why
# `escape_cs_string` deliberately skips it (see CodeField._escape_for_literal) —
# but nothing validated it either, so whatever the model wrote reached the file
# verbatim. Hit live on a ChronosDecay run, where the model generalised the
# prompt's own float example (`"default": "0f"`) to the integer case:
#
#     [SerializeField] private int _timelineIntegrityBreachCount = 0i;
#
# → SaveSystem.cs(16,175): CS1002, then two CS1519. `i` is not a C# numeric
# suffix; the entire valid set is f/d/m/u/l and the u+l combinations.
#
# Same doctrine as every other lint_* pass here: repair what is unambiguous,
# drop what is not, never raise. A dropped initializer still compiles — C#
# zero-initialises the field.

# Valid C# numeric literal suffixes, lower-cased. Trailing letters outside this
# set are invented and are a syntax error whatever the declared type is.
_CS_NUM_SUFFIXES  = frozenset({"", "f", "d", "m", "u", "l", "ul", "lu"})
_CS_INT_SUFFIXES  = frozenset({"u", "l", "ul", "lu"})
_INTEGRAL_TYPES   = frozenset({"int", "uint", "long", "ulong",
                               "short", "ushort", "byte", "sbyte"})
_HEX_BIN_LITERAL_RE = re.compile(r"^[+-]?0[xXbB][0-9a-fA-F_]+(?P<sfx>[A-Za-z]*)$")
_DEC_LITERAL_RE = re.compile(
    r"^(?P<num>[+-]?(?:\d[\d_]*(?:\.[\d_]*)?|\.[\d_]+))"
    r"(?P<exp>[eE][+-]?\d+)?"
    r"(?P<sfx>[A-Za-z]*)$")
_STRING_LITERAL_PREFIXES = ('"', '@"', '$"', '$@"', '@$"')
_STRING_SENTINELS = frozenset({"null", "string.empty", "default"})
_PY_LITERALS = {"none": "null", "true": "true", "false": "false"}


def _unescaped_quote_count(text: str) -> int:
    """Count `"` characters that actually open or close a literal (a `\\"` does
    neither). An odd total means the literal is never terminated."""
    count, i = 0, 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            count += 1
        i += 1
    return count


def _initializer_is_structurally_safe(text: str) -> bool:
    """True when `text` can stand as `= <text>;` without corrupting the file.

    The declaration is emitted on ONE line, so a newline, a stray `;` or an
    unclosed bracket does not merely mistype this field — it swallows or splits
    every member after it. Brackets and semicolons are judged at CODE positions
    only (`_code_mask`), so a `;` inside `"a;b"` is not held against it.
    """
    if "\n" in text or "\r" in text:
        return False
    if _unescaped_quote_count(text) % 2:
        return False                                   # unterminated literal
    mask = _code_mask(text)
    depth = {"(": 0, "[": 0, "{": 0}
    closers = {")": "(", "]": "[", "}": "{"}
    for i, ch in enumerate(text):
        if not mask[i]:
            continue
        if ch == ";":
            return False
        if ch in depth:
            depth[ch] += 1
        elif ch in closers:
            depth[closers[ch]] -= 1
            if depth[closers[ch]] < 0:
                return False
    return not any(depth.values())


def sanitize_default(type_str: str, default: Optional[str]) -> Optional[str]:
    """Coerce an LLM-authored initializer into a literal that parses as
    `= <default>;` for the declared type. Returns None to drop the initializer.

    Repairs, in order: Python literals (`None`/`True`), bool and string shapes,
    then numeric suffix/type agreement. An arbitrary expression
    (`new Vector3(0f, 1f, 0f)`, `Color.white`) cannot be retyped from here, so
    only the structural guard applies to it.
    """
    if default is None:
        return None
    text = default.strip()
    if not text:
        return None
    text = _PY_LITERALS.get(text.lower(), text)
    base = type_str.strip().rstrip("?").strip().lower()

    if base == "bool":
        if text in ("true", "false"):
            return text
        if text in ("1", "0"):                         # CS0029 — int is not bool
            return "true" if text == "1" else "false"
        return None

    if base == "string":
        if (text.lower() in _STRING_SENTINELS
                or text.startswith(_STRING_LITERAL_PREFIXES)):
            return text if _initializer_is_structurally_safe(text) else None
        # Bare prose the model forgot to quote (CS0103) — quoting keeps the
        # authored value; dropping it would lose the content for no reason.
        return f'"{escape_cs_string(text)}"'

    # Hex/binary must be tried FIRST: in `0xFF` a decimal-shaped match would
    # read the digits as an invented suffix and "repair" the value to 0.
    hexbin = _HEX_BIN_LITERAL_RE.match(text)
    if hexbin:
        mant, exp, sfx = text[: hexbin.start("sfx")], "", hexbin.group("sfx")
        fractional = False
    else:
        dec = _DEC_LITERAL_RE.match(text)
        if dec is None:
            # An expression (`new Vector3(0f, 1f, 0f)`, `Color.white`) — nothing
            # here can retype it, so only the structural guard applies.
            return text if _initializer_is_structurally_safe(text) else None
        mant, exp, sfx = dec.group("num"), dec.group("exp") or "", dec.group("sfx")
        # `2.` is not a C# real literal (`2.f` lexes as member access on an int),
        # so normalise the dangling separator away rather than suffixing it.
        if mant.endswith("."):
            mant = mant[:-1]
        fractional = bool(exp) or "." in mant

    # Classify case-insensitively, but re-emit the author's own casing — `5L`
    # must not come back as the far less readable `5l`.
    kind = sfx.lower()
    if kind not in _CS_NUM_SUFFIXES:
        kind, sfx = "", ""                             # invented — `0i`, `5int`

    if base in _INTEGRAL_TYPES:
        # `int x = 2.5;` is CS0266 and there is no non-guessing repair:
        # truncating invents a value, widening the field rewrites the author's
        # declared type. Drop the initializer and let the field zero-init.
        if fractional:
            return None
        return mant + exp + (sfx if kind in _CS_INT_SUFFIXES else "")
    if base in ("float", "single"):                    # `float x = 0.5;` is CS0664
        return mant + exp + (sfx if kind == "f" else "f")
    if base == "double":
        return mant + exp + (sfx if kind == "d" else "")
    if base == "decimal":
        return mant + exp + (sfx if kind == "m" else "m")
    # Unknown declared type (an enum, a struct, `var`): the suffix is the only
    # thing that is certainly wrong, so fix that and leave the value alone.
    return mant + exp + sfx


def lint_fields(fields: list[CodeField], *, reserved: set[str]) -> list[CodeField]:
    """Drop fields whose name collides with template-emitted names or duplicates,
    and repair initializers that would not parse (see `sanitize_default`)."""
    out: list[CodeField] = []
    seen: set[str] = set()
    for f in fields:
        key = f.name.lower()
        if key in reserved or key in seen:
            console.print(f"  [yellow]FIX[/]  dropped duplicate/reserved field {f.name!r}")
            continue
        seen.add(key)
        fixed = sanitize_default(f.type, f.default)
        if fixed != f.default:
            if fixed is None:
                console.print(f"  [yellow]FIX[/]  dropped uncompilable initializer "
                              f"{f.default!r} on `{f.type} {f.name}`")
            else:
                console.print(f"  [magenta]FIX[/]  repaired initializer on "
                              f"`{f.type} {f.name}`: {f.default!r} -> {fixed!r}")
            f = f.model_copy(update={"default": fixed})
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


class _ExtensibleLike(Protocol):
    """Structural shape lint_extensible needs. CodeExtensibleSchema satisfies
    this nominally; UnityScript (unity_schema.py) satisfies it structurally
    without a shared base class (importing it here would be circular — it
    imports CodeField/CodeMethod from this module)."""
    extra_usings:       list[str]
    extra_fields:       list[CodeField]
    awake_extra:        str
    update_extra:       str
    fixed_update_extra: str
    extra_methods:      list[CodeMethod]


def lint_extensible(schema: _ExtensibleLike, *,
                    reserved_fields: set[str],
                    template_usings: set[str]) -> _ExtensibleLike:
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
- An extra_field "default" is the raw initializer, so it must be a literal that
  is valid FOR THAT TYPE. The only C# numeric suffixes are f (float), d
  (double), m (decimal) and u/l (integer width) — there is no suffix for int,
  and no suffix is ever spelled with the type's first letter:
    float -> "0f", "1.5f"   int  -> "0", "12"       (NEVER "0i")
    double -> "0", "1.5"    bool -> "true"/"false"  (NEVER "True")
  A string default must be a QUOTED C# literal (the JSON value therefore
  contains the quote characters), or "null" — a bare word is not a literal.
- Every prompt has SOME prompt-specific behavior — the brief you were given
  describes it. Author at least one extra_field and one extra_method (or
  awake_extra/update_extra statements) that implement it; do not fall back to
  an empty shell. Only leave an extra_* empty when that specific piece truly
  does not apply (e.g. no per-frame behavior needed → update_extra stays "").

Add these keys to your JSON output (alongside the structural keys), authored
to reflect THIS prompt — the following is only a shape example, not a value
to copy verbatim:
  "extra_usings": ["UnityEngine.Events"],
  "extra_fields": [
    {{"name": "_cooldown", "type": "float", "default": "0f", "header": "Runtime"}}
  ],
  "awake_extra": "",
  "update_extra": "if (_cooldown > 0f) _cooldown -= Time.deltaTime;",
  "fixed_update_extra": "",
  "extra_methods": [
    {{"signature": "private bool IsReady()", "body": "return _cooldown <= 0f;"}}
  ]
"""


# ── GTA5 / SHVDN variant ──────────────────────────────────────────────────────

class GtaCodeExtensibleSchema(BaseModel):
    """
    SHVDN twin of CodeExtensibleSchema — the lifecycle names map onto a
    GTA.Script subclass instead of a MonoBehaviour:
      on_start_extra -> end of the Script constructor (runs once at load)
      on_tick_extra  -> end of OnTick() (runs every frame)
      on_abort_extra -> Aborted handler (script unload cleanup)
    """
    extra_usings:   list[str]        = Field(default_factory=list)
    extra_fields:   list[CodeField]  = Field(default_factory=list)
    on_start_extra: str              = ""
    on_tick_extra:  str              = ""
    on_abort_extra: str              = ""
    extra_methods:  list[CodeMethod] = Field(default_factory=list)


_GTA_KNOWN_NAMESPACES = frozenset({
    "System", "System.Collections.Generic", "System.Linq", "System.IO",
    "System.Windows.Forms", "GTA", "GTA.Math", "GTA.Native", "GTA.UI",
})


def lint_gta_extensible(schema: GtaCodeExtensibleSchema, *,
                        reserved_fields: set[str],
                        template_usings: set[str],
                        lookups_dir: str = "lookups") -> GtaCodeExtensibleSchema:
    """
    GTA twin of lint_extensible: the structural passes plus the full SHVDN
    reality lint (hash/member/enum allowlists + misuse neutralizers) over every
    fragment — the same battle-tested cleaning the mission pipeline applies to
    its extra_* channel.
    """
    from ..gta_allowlists import (
        load_hash_allowlist, load_api_surface, load_enum_allowlist,
    )
    hash_allow = load_hash_allowlist(lookups_dir)
    api_surface = load_api_surface(lookups_dir)
    enum_allow = load_enum_allowlist(lookups_dir)

    schema.extra_usings = lint_usings(schema.extra_usings, already_in_template=template_usings,
                                      allowed=_GTA_KNOWN_NAMESPACES)
    schema.extra_fields = lint_fields(schema.extra_fields, reserved=reserved_fields)
    schema.on_start_extra = lint_code_fragment(schema.on_start_extra, what="ctor body")
    schema.on_tick_extra  = lint_code_fragment(schema.on_tick_extra,  what="OnTick() body")
    schema.on_abort_extra = lint_code_fragment(schema.on_abort_extra, what="Aborted() body")
    taken: set[str] = set()
    cleaned: list[CodeMethod] = []
    for m in schema.extra_methods:
        m2 = lint_method(m, taken_names=taken)
        if m2 is not None:
            cleaned.append(m2)
    schema.extra_methods = cleaned

    def _shvdn_pass(fragment: str) -> str:
        fragment = lint_undeclared_catch_exception_var(fragment)
        fragment = lint_hash_references(fragment, allowed=hash_allow)
        fragment = lint_member_references(fragment, surface=api_surface)
        fragment = lint_enum_references(fragment, enums=enum_allow)
        fragment = lint_vector3_static_misuse(fragment)
        fragment = lint_bare_task_static_misuse(fragment)
        fragment = lint_function_call_ref_misuse(fragment)
        fragment = lint_world_get_out_param_misuse(fragment)
        fragment = lint_vector3_raw_call_arg(fragment)
        fragment = lint_vector3_entity_prop_misuse(fragment)
        return fragment

    schema.on_start_extra = _shvdn_pass(schema.on_start_extra)
    schema.on_tick_extra  = _shvdn_pass(schema.on_tick_extra)
    schema.on_abort_extra = _shvdn_pass(schema.on_abort_extra)
    for m in schema.extra_methods:
        m.body = _shvdn_pass(m.body)
    return schema


GTA_EXTRA_INSTRUCTIONS = """\

────────── PROMPT-SPECIFIC BEHAVIOR (this is how the prompt reaches the file) ──────────
The template already emits the BASELINE for the chosen variant — you do NOT
re-write it. You author the details unique to THIS prompt using the extra_* fields.
This is GTA5 / ScriptHookVDotNet 3 C# — NEVER UnityEngine:

  extra_usings:   extra namespaces, e.g. "System.Linq" (no "using", no ";")
  extra_fields:   extra private members the behavior needs
                  each: {{"name": "_myTimer", "type": "int", "default": "0", "tooltip": "..."}}
  on_start_extra: C# statements run ONCE when the script loads (end of constructor)
  on_tick_extra:  C# statements run EVERY frame (end of OnTick)
  on_abort_extra: cleanup statements when the script unloads (Aborted handler)
  extra_methods:  helper methods, each {{"signature": "private void Foo()", "body": "..."}}

SHVDN RULES:
- Use SHVDN3 API: Game.Player.Character, World.CreatePed, Function.Call(Hash.XXX, ...),
  GTA.UI.Notification.PostTicker("~y~text", false), GTA.Math.Vector3.
- Pure C#, never Python. No "using" lines inside bodies — those go in extra_usings.
- No braces around extra_methods bodies — the template adds them.
- An extra_field "default" is the raw initializer, so it must be a literal that
  is valid FOR THAT TYPE. The only C# numeric suffixes are f (float), d
  (double), m (decimal) and u/l (integer width) — there is no suffix for int,
  and no suffix is ever spelled with the type's first letter:
    float -> "0f", "1.5f"   int  -> "0", "12"       (NEVER "0i")
    double -> "0", "1.5"    bool -> "true"/"false"  (NEVER "True")
  A string default must be a QUOTED C# literal (the JSON value therefore
  contains the quote characters), or "null" — a bare word is not a literal.
- OnTick fires many times per SECOND — accumulate Game.LastFrameTime for any
  per-second meter/countdown instead of decrementing every tick.
- Every prompt has SOME prompt-specific behavior — the brief you were given
  describes it. Author at least one extra_field and one extra_method (or
  on_start_extra/on_tick_extra statements) that implement it; do not fall
  back to an empty shell. Only leave an extra_* empty when that specific
  piece truly does not apply (e.g. nothing to do at unload → on_abort_extra
  stays "").

Add these keys to your JSON output (alongside the structural keys), authored
to reflect THIS prompt — the following is only a shape example, not a value
to copy verbatim:
  "extra_usings": ["System.Linq"],
  "extra_fields": [
    {{"name": "_cooldown", "type": "int", "default": "0", "tooltip": "ticks remaining"}}
  ],
  "on_start_extra": "",
  "on_tick_extra": "if (_cooldown > 0) _cooldown--;",
  "on_abort_extra": "",
  "extra_methods": [
    {{"signature": "private bool IsReady()", "body": "return _cooldown <= 0;"}}
  ]
"""
