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
from functools import lru_cache
from pathlib import Path
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

# Reserved words a sanitised identifier must never become.
CSHARP_KEYWORDS = frozenset("""
abstract as base bool break byte case catch char checked class const continue
decimal default delegate do double else enum event explicit extern false finally
fixed float for foreach goto if implicit in int interface internal is lock long
namespace new null object operator out override params private protected public
readonly ref return sbyte sealed short sizeof stackalloc static string struct
switch this throw true try typeof uint ulong unchecked unsafe ushort using
virtual void volatile while
""".split())


def csharp_ident(s: str, fallback: str = "Value") -> str:
    """PascalCase C# identifier from arbitrary authored text.

    The single place the compiler turns a model's free-text name into something
    it can emit as an enum member, a field or a method name. Deliberately
    TOTAL — every input yields a usable identifier — because this is the
    Tier-V half of the name/role split (docs/LLM_ENRICHMENT_STRATEGY.md): the
    model's vocabulary must survive into the generated file, and rejecting a
    name here would put us back to filtering variety out.

    Not `to_pascal`: that returns an already-identifier-shaped string untouched,
    so a lowercase "oxygen" would stay lowercase and read as a field rather than
    a member — which is how `Setoxygen` was once emitted.
    """
    parts = re.sub(r"[^a-zA-Z0-9]+", " ", str(s)).split()
    out = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not out or out[0].isdigit():
        out = fallback + out
    if out.lower() in CSHARP_KEYWORDS:
        out += fallback          # "switch" -> SwitchState / SwitchValue
    return out


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

# Ceiling on members lifted out of bodies in one lint pass (see
# `_split_nested_members`). Real fragments hoist one or two; the bound only
# exists so a pathological fragment cannot spin the queue in `lint_extensible`.
_MAX_HOISTED_METHODS = 32


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


# Subset of `_ESCAPED_WS_RE` (defined below, alongside `_decode_escaped_
# whitespace`) — `\r\n`/`\n` only, deliberately NOT `\t`: a `\t` mid-comment
# doesn't end the comment in real C# either, so an undecoded one shouldn't
# end it here. See `_code_mask`'s docstring for why this exists.
_ESCAPED_NEWLINE_RE = re.compile(r"\\r\\n|\\n")


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

    A `//` comment also ends at an UNDECODED `\\n`/`\\r\\n` escape, not just
    a real newline. `_decode_escaped_whitespace` runs a normal masked scan to
    turn those back into real newlines — but it hasn't run yet the first time
    THIS function sees the fragment, and a bare `text.find("\\n", i)` cannot
    find a literal backslash-n. Hit live, DropZone's AudioManager.cs(47): a
    method body that mixed one real newline with five undecoded `\\n`
    escapes. The line-comment scan found no real `\\n` anywhere after the
    first `//`, ran to the end of the fragment, and masked the ENTIRE REST of
    the body — including its remaining braces and every escape after the
    first — as "inside a comment". That hid them from
    `_decode_escaped_whitespace` (mask says non-code, so it never decoded
    them) and from the brace counter (which then "balanced" a depth it could
    no longer see by appending a spurious `}`), corrupting a fragment that
    would otherwise have decoded and parsed cleanly. Chicken-and-egg by
    nature — an accurate mask needs the escapes decoded, decoding needs an
    accurate mask — so this function has to treat an undecoded escape as the
    same kind of line-ending a real newline is, rather than wait for a
    prior pass that has not run yet.
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
                esc = _ESCAPED_NEWLINE_RE.search(text, i)
                if esc and esc.start() < j:
                    j = esc.start()
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


def _code_paren_depth(text: str) -> int:
    """Net parenthesis depth counting only parens that are real CODE.

    Sibling of `_code_brace_depth`, over the same `_code_mask` and for the same
    reason: a `(` inside a string or a `//` comment is not a delimiter. An
    interpolated string is masked whole, so the parens inside an interpolation
    hole (`$"{Foo(1)}"`) correctly do not count either.
    """
    mask = _code_mask(text)
    depth = 0
    for i, ch in enumerate(text):
        if not mask[i]:
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
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
    the enclosing method the same way) is not guessed at here — dropping a brace
    can silently merge two blocks. That case no longer reaches this function:
    `lint_code_fragment` rejects an over-closed fragment outright, because
    deferring it to the syntax gate cost the whole build rather than one
    injection point (grandma run, EnemyFSM.cs).
    """
    depth = _code_brace_depth(text)
    if depth > 0:
        text = text + ("\n" + "}" * depth)
        console.print(f"  [magenta]FIX[/]  closed {depth} unterminated block(s) "
                      f"— brace in a comment/string is not a delimiter")
    return text


# CS1056 ("Unexpected character '\'"): a fragment that arrives with JSON escape
# sequences never decoded, so the whole body is one physical line carrying
# literal backslash-n. Hit live on a grandma run,
# WorldInteractable.cs(87,59):
#
#     if (string.IsNullOrEmpty(_t)) return;\n\nDebug.Log($"...");\n// comment
#
# Each `\` is a hard parse error and the trailing `//` then comments out
# everything that followed on that one line. Decode only in CODE regions —
# inside a C# string literal `\n` is a legitimate escape and must survive.
_ESCAPED_WS_RE = re.compile(r"\\r\\n|\\n|\\t")


def _decode_escaped_whitespace(text: str) -> str:
    """Turn undecoded `\\n`/`\\t` escapes back into real whitespace."""
    if "\\n" not in text and "\\t" not in text:
        return text
    mask = _code_mask(text)
    out: list[str] = []
    i = 0
    changed = False
    while i < len(text):
        m = _ESCAPED_WS_RE.match(text, i)
        if m and mask[i]:
            out.append("\t" if m.group(0) == "\\t" else "\n")
            i = m.end()
            changed = True
            continue
        out.append(text[i])
        i += 1
    if not changed:
        return text
    console.print("  [magenta]FIX[/]  decoded literal \\n escapes in a "
                  "fragment (CS1056)")
    return "".join(out)


# CS1002 ("; expected") / CS1010 ("Newline in constant") — one or two stray
# `"` characters glued directly onto the fragment with no separating token,
# at the very start and/or end. `""_airplaneAudioSource = ...;` parses as a
# complete, valid empty-string-literal EXPRESSION STATEMENT (`"";`)
# immediately followed by another token with no `;` between them — CS1002 at
# the exact column the identifier starts. A trailing `...;"` is the mirror
# shape: an unterminated string literal opening at that lone quote, which —
# because a string literal cannot itself contain a real newline — reports as
# CS1010 the moment the file's next line begins.
#
# No legitimate C# statement ever starts or ends with an isolated `"` glued
# straight onto an identifier with zero separation (a real string literal
# used as an expression is always followed by an operator, `.`, or `;`,
# never directly by another identifier character), so stripping it can only
# repair, never misfire on genuine code. Hit live, DropZone's
# AudioManager.cs — all three extra_methods on one custom script, each body
# glued with a leading `""`.
_LEADING_QUOTE_GLUE_RE  = re.compile(r'^"{1,2}(?=[A-Za-z_])')
_TRAILING_QUOTE_GLUE_RE = re.compile(r'(?<=[;}])"$')


def _repair_stray_quote_glue(text: str) -> str:
    """Strip a leading/trailing `"` (or `""`) with nothing legitimate for it
    to delimit — see the comment above."""
    out = text
    if _LEADING_QUOTE_GLUE_RE.search(out):
        out = _LEADING_QUOTE_GLUE_RE.sub("", out, count=1)
        console.print("  [magenta]FIX[/]  stripped a stray leading quote glued "
                      "onto a fragment (CS1002)")
    if _TRAILING_QUOTE_GLUE_RE.search(out):
        out = _TRAILING_QUOTE_GLUE_RE.sub("", out, count=1)
        console.print("  [magenta]FIX[/]  stripped a stray trailing quote glued "
                      "onto a fragment (CS1010)")
    return out


# CS1002/CS1519: C# accepts a fixed set of numeric-literal suffixes — u/U, l/L
# and their pairs for integers, f/F, d/D, m/M for reals. Models occasionally
# reach for one from another language. Hit live on a grandma run,
# PauseMenu.cs(18,173):
#
#     [SerializeField] private int _shopLayoutSeed = -1i;
#
# The stray `i` ends the declaration early (CS1002 "; expected") and the rest of
# the line then parses as two more bogus member declarations (CS1519 x2) — three
# diagnostics from one character. Drop the invalid suffix, keep the number.
_VALID_NUM_SUFFIXES = {"u", "l", "ul", "lu", "f", "d", "m"}
_NUM_SUFFIX_RE = re.compile(
    r"(?<![\w.])"                          # not mid-identifier
    r"(?!0[xXbB])"                         # 0x1F is one literal, not 0 + "x1F"
    r"(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"    # the literal
    r"([A-Za-z_]\w*)"                      # the suffix run
)


# CS0234 ("The type or namespace name 'X' does not exist in the namespace
# 'Y'"): a model reaches for a Cinemachine 3.x type but writes it fully
# qualified under a plausible-but-wrong URP namespace instead of Cinemachine's
# own. Hit live, GameSequencer.cs(114,86):
#
#     Camera.main?.GetComponent<UnityEngine.Rendering.Universal.
#         CinemachineImpulseSource>()
#
# `Unity.Cinemachine` is already a known/healed using (_KNOWN_NAMESPACES,
# _USING_TRIGGERS below both cover the BARE type name), but that machinery
# never fires here because the type is fully qualified inline — a `using`
# statement plays no part in resolving it. A bare find/replace on the wrong
# prefix is safe: nothing legitimate is ever qualified
# `UnityEngine.Rendering.Universal.Cinemachine*`, since Cinemachine ships its
# own separate package/namespace, not a member of URP's.
_WRONG_CINEMACHINE_FQN_RE = re.compile(
    r"\bUnityEngine\.Rendering\.Universal\.(Cinemachine\w*)\b")


def _repair_wrong_namespace_qualifiers(text: str) -> str:
    """Fix a small set of confirmed-live wrong fully-qualified type names."""
    if not text or "Cinemachine" not in text:
        return text
    new = _WRONG_CINEMACHINE_FQN_RE.sub(r"Unity.Cinemachine.\1", text)
    if new != text:
        console.print("  [magenta]FIX[/]  corrected wrong namespace on "
                       "Cinemachine type(s) (CS0234)")
    return new


# CS0176 ("Member 'X' cannot be accessed with an instance reference; qualify
# it with a type name instead"): a model calls a STATIC-only Unity API member
# through an instance variable, as if it were an instance method. Hit live,
# AudioDirectorAudioManager.cs(115,9) and (124,9):
#
#     var source = GetFreeSource();      // returns an AudioSource instance
#     source.PlayClipAtPoint(null, transform.position);
#
# `AudioSource.PlayClipAtPoint(AudioClip, Vector3, float)` is `static` on the
# real API — there is no instance overload — so the fix is to re-qualify the
# call with the type name rather than strip it: the intent (play a one-shot
# clip at a point) survives unchanged, only the receiver does not. Deliberately
# narrow to this one confirmed-live static member; a bare find/replace is safe
# because nothing legitimate calls `PlayClipAtPoint` any other way.
_STATIC_CALLED_AS_INSTANCE_RE = re.compile(r"\b(?!AudioSource\b)\w+\.(PlayClipAtPoint)\(")


def _repair_static_called_as_instance(text: str) -> str:
    """Re-qualify a call to a known STATIC-only member made via an instance."""
    if not text or "PlayClipAtPoint" not in text:
        return text
    new = _STATIC_CALLED_AS_INSTANCE_RE.sub(r"AudioSource.\1(", text)
    if new != text:
        console.print("  [magenta]FIX[/]  re-qualified static-only member "
                       "call with its type name (CS0176)")
    return new


_RIGIDBODY2D_LOCAL_RE = re.compile(
    r'\b(?:var|Rigidbody2D)\s+(\w+)\s*=\s*(?:\([^()]*\)\s*)?GetComponent<Rigidbody2D>\s*\(\s*\)'
)


def _repair_forcemode_on_rigidbody2d(text: str) -> str:
    """Rigidbody2D.AddForce/AddForceAtPosition/AddTorque take ForceMode2D,
    not ForceMode — same enum member NAMES (Impulse/Force/VelocityChange/
    Acceleration), different types (CS1503). Confirmed live (2026-08-28,
    WorldInteractable.cs(105)):
        thrown.GetComponent<Rigidbody2D>().AddForce(Vector2.right * 15f, ForceMode.Impulse);
    A straightforward 3D-physics-API-on-a-2D-body slip. Tracks both the
    direct GetComponent<Rigidbody2D>() chain and a `var rb =
    GetComponent<Rigidbody2D>();` ... `rb.AddForce(..., ForceMode.X)`
    two-step form (the same locals-tracking shape
    _repair_gameobject_component_assign uses), since either is equally
    plausible LLM output."""
    if not text or "ForceMode." not in text or "Rigidbody2D" not in text:
        return text
    names = sorted({m.group(1) for m in _RIGIDBODY2D_LOCAL_RE.finditer(text)},
                    key=len, reverse=True)
    receivers = [r'GetComponent<Rigidbody2D>\s*\(\s*\)'] + [re.escape(n) for n in names]
    pattern = re.compile(
        r'(?:' + '|'.join(receivers) + r')\s*\.\s*(?:AddForce|AddForceAtPosition|AddTorque)\s*\('
        r'[^;]*?\bForceMode\.(?:Impulse|Force|VelocityChange|Acceleration)\b'
    )
    member_re = re.compile(r'\bForceMode\.(Impulse|Force|VelocityChange|Acceleration)\b')

    def _fix_call(m: "re.Match[str]") -> str:
        return member_re.sub(r'ForceMode2D.\1', m.group(0))

    new = pattern.sub(_fix_call, text)
    if new != text:
        console.print("  [magenta]FIX[/]  corrected ForceMode -> ForceMode2D on a "
                       "Rigidbody2D call (CS1503)")
    return new


def _repair_self_class_static_call(text: str, self_class_name: str) -> str:
    """`<ThisClass>.SomeMethod(...)` written from within `ThisClass` itself
    (CS0120 when SomeMethod is an instance member — a model reaching for
    Java/other-language "call it by class" habits, or just confusing static
    and instance). Confirmed live (2026-08-28), GameRules.cs(113):
        if (...) { GameRules.Begin(); ... }
    called from inside class GameRules itself, where Begin() is `public void
    Begin()` — a genuine instance method. Stripping the redundant
    self-qualifier is safe unconditionally, not just for this case: if
    SomeMethod were actually static, C# resolves a bare call to it from
    within the same class identically to the qualified form, so there is no
    behavior this could ever change for a correct static call — only fix an
    incorrect instance one. Deliberately does NOT try to guess a "more
    correct" method to call instead (e.g. a sibling SpawnEnemy() that might
    have been intended) — that is a content judgment belonging to the
    schema-fill step, not a mechanical repair."""
    if not text or not self_class_name or self_class_name not in text:
        return text
    pattern = re.compile(r'\b' + re.escape(self_class_name) + r'\.(?=\w+\s*\()')
    new = pattern.sub("", text)
    if new != text:
        console.print(f"  [magenta]FIX[/]  dropped redundant self-class "
                      f"qualifier '{self_class_name}.' before an instance "
                      f"call (CS0120)")
    return new


def _repair_collection_compared_as_scalar(text: str, field_types: "dict[str, str]") -> str:
    """A List<T>/array-typed TEMPLATE field compared directly via a
    relational operator (CS0019 — List<T> has no such operator overload at
    all, so this is unconditionally wrong, never a legitimate call).
    Confirmed live (2026-08-28), EnemySpawner.cs(161):
        return _spawnTimer <= 0f && _alive < maxAlive;
    where `_alive` is the template's own `private readonly List<GameObject>
    _alive`, used correctly as `_alive.Count` everywhere else in that SAME
    file — the LLM's own IsSpawningNextWave() clearly meant `_alive.Count <
    maxAlive`. Rewriting (not neutralizing, unlike the older
    find_field_type_misuse member-access checks) because there is no second
    interpretation for a bare relational operator on a collection, the way
    there can be for an ambiguous member call. Scoped to the observed
    direction (collection on the left of the operator) — the field_types
    dict comes from the same template_field_types/extra_fields source
    lint_field_type_misuse already uses, no new plumbing."""
    if not text or not field_types:
        return text
    collection_members: dict[str, str] = {}
    for name, ftype in field_types.items():
        t = (ftype or "").strip()
        if t.endswith("[]"):
            collection_members[name] = "Length"
        elif re.match(r'^(?:List|IList|ICollection|IReadOnlyList|IReadOnlyCollection)\s*<', t):
            collection_members[name] = "Count"
    if not collection_members:
        return text

    changed = False
    for name, member in collection_members.items():
        pattern = re.compile(r'\b' + re.escape(name) + r'\b(?!\s*\.\w)(\s*(?:<=|>=|<|>)\s*)')
        new_text, n = pattern.subn(f'{name}.{member}' + r'\1', text)
        if n:
            text = new_text
            changed = True
    if changed:
        console.print("  [magenta]FIX[/]  inserted .Count/.Length before a "
                      "relational operator on a collection field (CS0019)")
    return text


def _repair_animator_play_layer_arg(text: str, field_types: "dict[str, str]") -> str:
    """Animator.Play/PlayInFixedTime's SECOND positional argument is `int
    layer`, unlike CrossFade/CrossFadeInFixedTime where position 2 is a
    float duration (_ANIMATOR_STATE_SWITCH_METHODS's own comment treats all
    four as one shape — Play's real signature is (stateName, int layer,
    float normalizedTime); CrossFade's is (stateName, float duration, int
    layer, ...) — different order). Confirmed live (2026-08-28),
    PlayerAnimator.cs(104):
        _anim.Play(_play_J_, -1f, 0f);
    `-1f` is Unity's own idiomatic "current layer" sentinel, just written
    with an errant `f` suffix (CS1503) — the existing string-argument check
    next to this one never sees it, since a float isn't a string. Rewrites
    (drops the suffix and any decimal remainder) rather than neutralizing:
    a genuine float literal in Play's layer slot is provably wrong
    regardless of overload, so there is nothing ambiguous to guess at."""
    if not text or not field_types or ".Play" not in text:
        return text
    animator_names = {n for n, t in field_types.items()
                      if (t or "").strip().lower() == "animator"}
    if not animator_names:
        return text
    names_alt = "|".join(re.escape(n) for n in sorted(animator_names, key=len, reverse=True))
    pattern = re.compile(
        r'(\b(?:' + names_alt + r')\.(?:Play|PlayInFixedTime)\s*\([^,()]*,\s*)'
        r'(-?\d+)\.?\d*f\b'
    )
    new = pattern.sub(r'\1\2', text)
    if new != text:
        console.print("  [magenta]FIX[/]  corrected Animator.Play's int layer "
                      "argument (float literal -> int, CS1503)")
    return new


_STRUCT_RETURN_TYPES = frozenset({"vector2", "vector3", "vector4", "color", "color32", "quaternion"})
_METHOD_RETURN_TYPE_RE = re.compile(
    r'\b(?:public|private|protected|internal)\b(?:\s+(?:static|virtual|override|async|sealed))*'
    r'\s+([A-Za-z_]\w*)\s+[A-Za-z_]\w*\s*\(')


def _repair_struct_return_type_mismatch(text: str, field_types: "dict[str, str]",
                                        signature: str = "") -> str:
    """A `return` statement referencing a field whose declared struct type
    doesn't match the method's own declared return type (CS0029 — no
    implicit conversion between Vector2/3/4/Color/Quaternion). Confirmed
    live (2026-08-28), LightingDirector.cs, GetSkyboxColor():
        public Color GetSkyboxColor()
        { return _skyboxColor != null ? _skyboxColor : ambientSky; }
    `_skyboxColor` is the LLM's own extra_field, declared Vector3 where the
    method (and the sibling TEMPLATE field it's ternary'd against,
    `ambientSky: Color`) both want Color — a wrong TYPE CHOICE for a new
    field, not a fixable syntax slip, so this neutralizes the return
    statement rather than guessing a conversion. A method left with no live
    return statement is then dropped whole by lint_method's own dead-body
    check — safe here specifically because nothing else in a freshly
    generated project can already be calling a method the LLM just invented
    this run."""
    if not text or "return" not in text or not field_types:
        return text
    m = _METHOD_RETURN_TYPE_RE.search(signature or "")
    if not m:
        return text
    ret_type = m.group(1).strip().lower()
    if ret_type not in _STRUCT_RETURN_TYPES:
        return text
    out_lines = []
    changed = False
    for line in text.splitlines():
        if "return" not in line:
            out_lines.append(line)
            continue
        mismatched = any(
            (ftype or "").strip().lower() in _STRUCT_RETURN_TYPES
            and (ftype or "").strip().lower() != ret_type
            for name, ftype in field_types.items()
            if re.search(r'\b' + re.escape(name) + r'\b', line)
        )
        if mismatched:
            indent = line[:len(line) - len(line.lstrip())]
            out_lines.append(f"{indent}// AUDIT: removed type-incompatible return -- {line.strip()}")
            changed = True
        else:
            out_lines.append(line)
    if not changed:
        return text
    console.print("  [magenta]FIX[/]  neutralized a return statement referencing a "
                  "struct-typed field incompatible with the method's own return "
                  "type (CS0029)")
    return "\n".join(out_lines)


_ASSIGNMENT_CALL_RE = re.compile(
    r'\b(?P<field>[A-Za-z_]\w*)\s*=\s*(?P<call>[A-Za-z_]\w*)\s*\(')
# Type pairs where an implicit conversion IS legal, so two DIFFERENT type
# names alone must not be enough to flag — numeric widening is the only such
# pair either side of this check could plausibly produce (every other pair a
# bareword type-name regex can see is between distinct reference/struct
# types, which C# never converts implicitly).
_ASSIGNMENT_WIDENING_PAIRS = frozenset({
    ("int", "float"), ("int", "double"), ("float", "double"),
})


def _repair_field_assignment_type_mismatch(
        text: str, *, field_types: "dict[str, str]",
        method_return_types: "dict[str, str]") -> str:
    """A field is assigned the return value of a template-native method whose
    OWN declared return type does not match the field's OWN declared type —
    CS0029, no implicit conversion between two unrelated types. Confirmed live
    (2026-08-30), LightingDirector.cs:
        _rigRoot = FindBrightestDirectional();
    `_rigRoot` is the template's own `Transform`; `FindBrightestDirectional()`
    is the template's own `private static Light`. The author's OWN field
    (whatever type it actually declared this under) was already dropped as a
    reserved-name duplicate by `lint_fields` — a NAME-only collision check
    with no way to know the usage disagreed on TYPE — so this is the other
    half: check the surviving, template-typed field against what its authored
    assignment actually hands it.

    Scoped to bareword type names on both sides (no generics/interfaces to
    reason about — a mismatch there is `field_types`/`method_return_types`
    simply not having an entry, which this silently skips) and exempts
    numeric widening, the only legal implicit conversion two textually
    different simple type names could still have."""
    if not text or not field_types or not method_return_types:
        return text
    out_lines = []
    changed = False
    for line in text.splitlines():
        m = _ASSIGNMENT_CALL_RE.search(line)
        ftype = field_types.get(m.group("field")) if m else None
        rtype = method_return_types.get(m.group("call")) if m else None
        if ftype and rtype:
            fnorm, rnorm = ftype.strip().lower(), rtype.strip().lower()
            mismatched = (fnorm != rnorm
                         and (fnorm, rnorm) not in _ASSIGNMENT_WIDENING_PAIRS
                         and (rnorm, fnorm) not in _ASSIGNMENT_WIDENING_PAIRS)
        else:
            mismatched = False
        if mismatched and m is not None:
            indent = line[:len(line) - len(line.lstrip())]
            out_lines.append(
                f"{indent}// AUDIT: removed assignment -- {line.strip()} "
                f"({m.group('field')} is {ftype}, {m.group('call')}() "
                f"returns {rtype}, CS0029)")
            changed = True
        else:
            out_lines.append(line)
    if not changed:
        return text
    console.print("  [magenta]FIX[/]  neutralized a field assignment whose "
                  "declared type disagrees with the called method's own "
                  "return type (CS0029)")
    return "\n".join(out_lines)


def _repair_getcomponent_wrong_dimension(text: str, field_types: "dict[str, str]") -> str:
    """A field with a known 2D-physics type assigned from GetComponent<X>()
    where X is that type's 3D counterpart (CS0029 — Rigidbody has no
    implicit conversion to Rigidbody2D, and likewise for Collider/
    BoxCollider/etc.). Confirmed live (2026-08-28), Bike.cs(36) (this
    session's own bike micro — LLM-authored awake_extra assigning its own
    template field `_riderRb: Rigidbody2D`):
        _riderRb = _mountPoint.GetComponent<Rigidbody>();
    Rewrites the generic argument to match the field's real declared type —
    unambiguous, the same "only one sensible reading" reasoning as
    _repair_forcemode_on_rigidbody2d. General over any `<X>2D`-suffixed
    field type, not just Rigidbody2D — Collider2D/BoxCollider2D/etc. share
    the exact same 3D-vs-2D confusion shape."""
    if not text or "GetComponent<" not in text or not field_types:
        return text
    changed = False
    for name, ftype in field_types.items():
        t = (ftype or "").strip()
        if not t.endswith("2D") or len(t) <= 2:
            continue
        three_d = t[:-2]   # "Rigidbody2D" -> "Rigidbody", "BoxCollider2D" -> "BoxCollider"
        pattern = re.compile(
            r'\b' + re.escape(name) + r'\b\s*=\s*[^;]*?GetComponent<'
            + re.escape(three_d) + r'>\s*\(\s*\)')

        def _fix(m: "re.Match[str]", _three_d=three_d, _t=t) -> str:
            return m.group(0).replace(f"GetComponent<{_three_d}>", f"GetComponent<{_t}>")

        new_text, n = pattern.subn(_fix, text)
        if n:
            text = new_text
            changed = True
    if changed:
        console.print("  [magenta]FIX[/]  corrected GetComponent<T>()'s dimension to "
                      "match the field's own 2D-typed declaration (CS0029)")
    return text


def _repair_numeric_literals(text: str) -> str:
    """Strip numeric-literal suffixes C# does not accept.

    A digits-then-letters run can only be a literal plus a suffix — C# has no
    identifier starting with a digit — so this cannot corrupt a name. Hex and
    binary literals are excluded outright: without that guard `0x1F` reads as
    the literal `0` followed by the bogus suffix `x1F` and loses its value.
    """
    if not text:
        return text
    mask = _code_mask(text)
    changed = False

    def repl(m: "re.Match[str]") -> str:
        nonlocal changed
        if not mask[m.start(2)]:            # inside a comment or string literal
            return m.group(0)
        if m.group(2).lower() in _VALID_NUM_SUFFIXES:
            return m.group(0)
        changed = True
        return m.group(1)

    new = _NUM_SUFFIX_RE.sub(repl, text)
    if changed:
        console.print("  [magenta]FIX[/]  dropped invalid numeric literal "
                      "suffix (CS1002)")
    return new


# CS0106: a member declaration nested INSIDE a method body. C# allows a local
# FUNCTION there, but not an access modifier on it, so every `private`/`public`
# helper the model tucks inside another method raises CS0106 — and the parser
# then loses the class, cascading into CS1513/CS8803/CS1022. Hit live on the
# grandma run, PlayerFSM.cs(70,2) — 11 of the run's 14 diagnostics traced to one
# fragment:
#
#     private void OnRewindInputPressed()
#     {
#         if (Input.GetButtonDown(_rewindInputAxisName))
#         { Invoke(nameof(TriggerRewind), 0.1f); }
#
#         private void TriggerRewind()          <- illegal here
#         { _temporalIntegrityMeter -= 5f; }
#     }
#
# Note the brace CHARACTERS balance, so `_balance_braces` correctly sees nothing
# to do — this is a placement defect, not a counting one.
#
# The repair HOISTS the nested member out to class level rather than demoting it
# to a local function by dropping the modifier. Demotion would compile and then
# fail at runtime: `Invoke(nameof(TriggerRewind), 0.1f)` resolves by reflection
# over the component's real methods, and a local function is not one. Hoisting
# preserves both call sites and semantics.
_ACCESS_MOD_HINT_RE = re.compile(r"\b(?:public|private|protected|internal)\b")
_NESTED_MEMBER_RE = re.compile(
    r"^[ \t]*(?:\[[^\]\n]*\][ \t]*)*"
    r"(?:public|private|protected|internal)\b"
    r"(?:[ \t]+(?:static|virtual|override|sealed|async|extern|new|unsafe|partial))*"
    r"[ \t]+[A-Za-z_][\w<>,\[\].?]*"       # return type
    r"[ \t]+[A-Za-z_]\w*[ \t]*\("          # name (
)


def _member_block_end(text: str, mask: bytearray, start: int) -> Optional[int]:
    """Index just past a member declaration beginning at `start`, or None."""
    depth = 0
    opened = False
    for j in range(start, len(text)):
        if not mask[j]:
            continue
        ch = text[j]
        if ch == "{":
            depth += 1
            opened = True
        elif ch == "}":
            depth -= 1
            if opened and depth == 0:
                return j + 1
        elif ch == ";" and not opened:
            return j + 1                    # abstract / expression-bodied
    return None


def _split_member_block(block: str) -> tuple[str, str]:
    """Split a captured member into (signature, body)."""
    brace = block.find("{")
    if brace < 0:
        return "", ""
    close = block.rfind("}")
    if close < brace:
        return "", ""
    return block[:brace].strip(), block[brace + 1:close].strip()


def _split_nested_members(text: str) -> tuple[str, list[CodeMethod]]:
    """Lift member declarations the model nested inside a body fragment.

    Returns `(remaining_body, hoisted)`. A fragment is a method body, so its
    top level (depth 0) is already inside a method — an access-modified member
    there is the CS0106 defect. Anything deeper is inside a nested block and is
    left alone. If a captured member's braces don't close, nothing is changed
    and the syntax gate reports it rather than this pass guessing.
    """
    if not text or not _ACCESS_MOD_HINT_RE.search(text):
        return text, []
    mask = _code_mask(text)
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln)

    kept: list[str] = []
    hoisted: list[CodeMethod] = []
    depth = 0
    i = 0
    while i < len(lines):
        start = offsets[i]
        if depth == 0 and _NESTED_MEMBER_RE.match(lines[i]):
            end = _member_block_end(text, mask, start)
            if end is None:
                return text, []             # unbalanced — leave it to the gate
            sig, body = _split_member_block(text[start:end])
            if sig:
                hoisted.append(CodeMethod(signature=sig, body=body))
                while i < len(lines) and offsets[i] < end:
                    i += 1
                continue
        line = lines[i]
        for k in range(start, start + len(line)):
            if mask[k]:
                if text[k] == "{":
                    depth += 1
                elif text[k] == "}":
                    depth -= 1
        kept.append(line)
        i += 1

    if not hoisted:
        return text, []
    console.print(f"  [magenta]FIX[/]  hoisted {len(hoisted)} nested member "
                  f"declaration(s) to class level (CS0106)")
    return "".join(kept).strip(), hoisted


def _unclosed_braces(text: str) -> int:
    """How many `{` the text leaves open, ignoring strings and comments."""
    depth = 0
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"' or ch == "'":
            quote = ch
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    break
                i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return max(0, depth)


def _cap_lines(text: str, *, what: str) -> str:
    """Cap a runaway body, leaving code that still PARSES.

    The cap slices at a line count, and a line count knows nothing about brace
    depth: cutting inside a `for` loop drops that block's closing brace and the
    method's with it, so the file ends mid-block and Unity reports CS1022 on the
    whole assembly. The syntax gate then rejects the entire build — one runaway
    method costing every other script in it.

    Hit live on Gridfall's GridLogic, where the model degenerated into repeating
    one `for` block and the cut landed inside the last copy. Closing what the
    slice left open turns an unparseable file into a truthful one: the body is
    still incomplete, and the TODO says so, but everything around it compiles.
    """
    lines = text.splitlines()
    if len(lines) <= _MAX_METHOD_LINES:
        return text
    kept = lines[: _MAX_METHOD_LINES - 1]
    kept.append(f"// TODO: continue — {what} truncated at {_MAX_METHOD_LINES} lines")
    open_blocks = _unclosed_braces("\n".join(kept))
    kept.extend("}" for _ in range(open_blocks))
    return "\n".join(kept)


# CS1612: a value-type property returns a COPY, so mutating a member of it
# (`transform.position.x += v`, `Physics.gravity.y = v`) does not compile. LLMs
# write this constantly. Repair to read-modify-write on a local.
_STRUCT_MEMBER_ASSIGN_RE = re.compile(
    r'((?:[A-Za-z_]\w*\.)*'
    r'(?:transform\.(?:position|localPosition|localScale|eulerAngles|localEulerAngles)'
    r'|Physics2D\.gravity|Physics\.gravity))'
    r'\.([xyzw])\s*(\+=|-=|\*=|/=|=(?!=))\s*([^;]+?)\s*;')


# CS0131: `_thing?.member = value;` — a null-conditional on the LEFT of an
# assignment. C# allows `?.` to READ through a possible null and not to write
# through one, so this is a compile error rather than a null-safety idiom.
# Models reach for it constantly because it reads like the safe thing to do,
# and it is the one place where what they MEANT is completely unambiguous: a
# null guard. Hit live on the TowerControl run, PlayerCamera.cs(128,9):
#
#     _radarSweep?.localRotation = Quaternion.Euler(0f, Time.time * _speed, 0f);
#
# Rewritten to the guard it was reaching for, which preserves the author's
# intent exactly — unlike neutralizing the line, which would silently delete a
# working piece of the game's behaviour.
_NULL_COND_ASSIGN_RE = re.compile(
    r'^(?P<indent>[ \t]*)(?P<recv>[A-Za-z_][A-Za-z0-9_]*)\?\.'
    r'(?P<tail>[A-Za-z_][A-Za-z0-9_.]*)\s*(?P<op>=|\+=|-=|\*=|/=)\s*'
    r'(?P<rhs>.+;)\s*$',
    re.MULTILINE)


def _repair_null_conditional_assign(text: str) -> str:
    """Rewrite `x?.y = v;` → `if (x != null) x.y = v;` (CS0131)."""
    new = _NULL_COND_ASSIGN_RE.sub(
        lambda m: (f"{m['indent']}if ({m['recv']} != null) "
                   f"{m['recv']}.{m['tail']} {m['op']} {m['rhs']}"),
        text)
    if new != text:
        console.print("  [magenta]FIX[/]  rewrote null-conditional assignment "
                      "as a null guard (CS0131)")
    return new


def _repair_struct_member_assign(text: str) -> str:
    """Rewrite `prop.x += v;` → `{ var _v = prop; _v.x += v; prop = _v; }` for the
    common value-type Unity properties that otherwise throw CS1612."""
    new = _STRUCT_MEMBER_ASSIGN_RE.sub(r'{ var _v = \1; _v.\2 \3 \4; \1 = _v; }', text)
    if new != text:
        console.print("  [magenta]FIX[/]  rewrote value-type property member assignment (CS1612)")
    return new


# CS0029: a Component-returning lookup (GetComponentInChildren<Canvas>(), ...)
# assigned to a field the model itself declared GameObject. Every Component
# exposes `.gameObject`, so — unlike the colour repair above — there is
# exactly one correct fix here, not a guess: append `.gameObject` to the call.
# Scoped to fields the model declared itself (extra_fields) since that is the
# only place this module has a field's TYPE to check against; a
# template-declared field would need a second lookup this pass does not have.
# Hit live on RepoFlow's `transition` micro, SceneTransition.cs(71):
# `_fadeCanvas` was declared `GameObject` and then assigned both
# `GetComponentInChildren<Canvas>()` and `FindObjectOfType<Canvas>()` from the
# same authored OnEnable().
_COMPONENT_LOOKUP_CALL_RE = (
    r"(?:GetComponentInChildren|GetComponentInParent|GetComponent|"
    r"FindAnyObjectByType|FindFirstObjectByType|FindObjectOfType)"
    r"\s*<\s*(?P<type>[A-Za-z_]\w*)\s*>\s*\(\s*\)")


def _repair_gameobject_component_assign(text: str,
                                        gameobject_fields: "frozenset[str]") -> str:
    """`<gameObjectField> = GetComponentInChildren<Canvas>();` -> append
    `?.gameObject` (CS0029). `?.`, not `.` — the lookup can legitimately
    return null (no matching component found), and `.gameObject` on a null
    reference is a NullReferenceException the original code never risked (it
    was assigning the possibly-null reference itself, not dereferencing it).
    No-op when there are no GameObject-typed fields to check against, or the
    looked-up type IS GameObject (already correct)."""
    if not text or not gameobject_fields:
        return text
    names = "|".join(re.escape(n) for n in
                     sorted(gameobject_fields, key=len, reverse=True))
    pattern = re.compile(
        rf"(?<![\w.])(?P<field>{names})(?P<sp1>\s*)=(?!=)(?P<sp2>\s*)"
        rf"(?P<call>{_COMPONENT_LOOKUP_CALL_RE})(?P<term>\s*;)")
    changed = False

    def repl(m: "re.Match") -> str:
        nonlocal changed
        if m["type"] == "GameObject":
            return m.group(0)               # already the right type
        changed = True
        return f"{m['field']}{m['sp1']}={m['sp2']}{m['call']}?.gameObject{m['term']}"

    new = pattern.sub(repl, text)
    if changed:
        console.print("  [magenta]FIX[/]  appended ?.gameObject to a Component "
                     "lookup assigned to a GameObject-typed field (CS0029)")
    return new


# CS0023: `!` cannot apply to a non-bool-valued expression. Unary `!` binds
# tighter than a comparison operator, so a model checking a numeric condition
# by negating the wrong term parses `!x <op> N` as `(!x) <op> N`, never the
# intended `!(x <op> N)`. Hit live twice: NPCDialogueManager.cs(17)
# `!_dialogueQueue.Count == 0` (meant `.Count != 0`), and StreetBeat2D's
# MovingPlatform.cs(137) `!_cooldown <= 0f` (meant `_cooldown > 0f`) — a bare
# field, not a `.Count`/`.Length` member, and a `<=` operator, not `==`/`!=`,
# so the original Count/Length-only, `==`/`!=`-only version missed it.
# Scoped to comparisons against a NUMERIC LITERAL specifically — that alone
# proves `!` is misapplied regardless of the left side's real type: if it were
# legitimately bool, `bool <op> <number>` could never have compiled in the
# first place (CS0019), `!` misplaced or not. Flipping the operator (De
# Morgan) instead of just dropping the `!` preserves the author's intent.
_BANG_NUMERIC_COMPARISON_RE = re.compile(
    r'!\s*([\w.]+)\s*(<=|>=|==|!=|<|>)\s*(-?\d+\.?\d*f?)\b')
_COMPARISON_NEGATION: dict[str, str] = {
    "==": "!=", "!=": "==", "<=": ">", ">=": "<", "<": ">=", ">": "<=",
}


def _repair_bang_count_comparison(text: str) -> str:
    """Rewrite `!x <op> N` -> `x <negated-op> N` for a numeric literal N
    (CS0023) — e.g. `!x.Count == 0` -> `x.Count != 0`, `!_cooldown <= 0f` ->
    `_cooldown > 0f`."""
    if not text or "!" not in text:
        return text
    mask = _code_mask(text)
    changed = False

    def repl(m: "re.Match[str]") -> str:
        nonlocal changed
        if not mask[m.start()]:
            return m.group(0)                       # inside a comment/literal
        changed = True
        flipped = _COMPARISON_NEGATION[m.group(2)]
        return f"{m.group(1)} {flipped} {m.group(3)}"

    new = _BANG_NUMERIC_COMPARISON_RE.sub(repl, text)
    if changed:
        console.print("  [magenta]FIX[/]  rewrote '!' applied to a numeric "
                      "comparison (CS0023)")
    return new


# CS0023 — a second, unrelated way `!` lands on a non-bool expression: negating
# a call to a template-declared method whose return type is `void`. The model
# treats a state-setting helper as if it answered a yes/no question. Confirmed
# live, DropZone's PauseMenu.cs(151):
#
#     private void OnPauseStateChanged()
#     {
#         _isPaused = !SetPaused(true);
#     }
#
# `SetPaused(bool)` (PauseMenu.cs.j2's own helper) just sets `Time.timeScale`
# and returns nothing. Unlike the numeric-comparison misparse above, there is
# no De Morgan flip that recovers the intended condition — the model invented
# a return value that was never there, so this is judged for neutralization,
# same as every other invented-member case in this module, rather than
# guessed at.
_BANG_CALL_RE = re.compile(r"!\s*([A-Za-z_]\w*)\s*\(")


def find_void_method_negated(text: str, *, void_methods: "frozenset[str]"
                             ) -> "dict[int, set[str]]":
    """`{line_index: {"!MethodName(...)", ...}}` for `!` applied to a call of
    a template-declared method whose return type is `void` (CS0023).
    `void_methods` is `template_void_methods()` — the same template scan
    `template_method_return_types` draws its (non-void) half from."""
    if not text or "!" not in text or not void_methods:
        return {}
    mask = _code_mask(text)
    bad: "dict[int, set[str]]" = {}
    for m in _BANG_CALL_RE.finditer(text):
        if not mask[m.start()]:
            continue
        name = m.group(1)
        if name not in void_methods:
            continue
        line = text.count("\n", 0, m.start())
        bad.setdefault(line, set()).add(f"!{name}(...)")
    return bad


def lint_void_method_negated(text: str, *, void_methods: "frozenset[str]"
                             ) -> "tuple[str, set[str]]":
    """Neutralize `!` applied to a known-void method call. Same three-way
    repair as every other `lint_*` pass here."""
    bad = find_void_method_negated(text, void_methods=void_methods)
    if not bad:
        return text, set()
    return _neutralize_flagged_lines(
        text, bad, reason="'!' applied to a void method's result")


# CS0103: LLMs write the pre-C#7 raycast idiom `Physics.Raycast(ray, out hit, …)`
# without ever declaring `hit`. Modern C# allows an inline `out var hit`, so
# declare the out parameter in place. Only lowercase, non-field (`_`-prefixed)
# identifiers that aren't already declared elsewhere in the fragment are
# touched; a second `out hit` reuses the first's declaration.
#
# The lookahead must also exclude lowercase C# primitive keywords, not just
# `var`/`ref` -- confirmed live: `float.TryParse(tagText, out float value)` is
# already a fully explicit, valid declaration, but the old lookahead let
# `float` itself match as the "undeclared identifier" (it's lowercase, and
# wasn't `var`/`ref`), rewriting it to `out var float value` — CS1003/CS1525.
_OUT_PRIMITIVE_KEYWORDS = (
    "var", "ref", "bool", "byte", "sbyte", "char", "decimal", "double",
    "float", "int", "uint", "long", "ulong", "short", "ushort", "object",
    "string", "dynamic",
)
_OUT_UNDECLARED_RE = re.compile(
    r'\bout\s+(?!(?:' + "|".join(_OUT_PRIMITIVE_KEYWORDS) + r')\b)'
    r'([a-z][A-Za-z0-9_]*)\b')


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


def lint_code_fragment(text: str, *, what: str,
                       hoist_into: Optional[list[CodeMethod]] = None,
                       gameobject_fields: "frozenset[str]" = frozenset(),
                       self_class_name: str = "") -> str:
    """
    Clean an LLM-authored C# fragment intended for an injection point.
    Returns "" if the fragment is rejected (caller should skip it).

    `hoist_into`, when given, receives any member declarations the model nested
    inside the body (CS0106) so the caller can re-home them at class level.
    Callers without a class-level channel omit it and the fragment is left as
    authored for the syntax gate to report.

    `gameobject_fields` names the schema's own GameObject-typed extra_fields,
    so a `<field> = GetComponentInChildren<Canvas>();` assigned to one of them
    can be repaired — see `_repair_gameobject_component_assign`.

    `self_class_name` (typically the schema's own `.name`) lets
    `_repair_self_class_static_call` drop a redundant `<ThisClass>.` before
    an instance-method call (CS0120) written from within that same class.
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
    # Before anything line-oriented: an undecoded `\n` makes the whole fragment
    # one physical line, which hides its structure from every pass below.
    text = _decode_escaped_whitespace(text)
    text = _repair_stray_quote_glue(text)
    # Strip `using X;` lines — these belong in `extra_usings`, not the body.
    text = _USING_RE.sub("", text)
    # Before any brace-sensitive pass: a nested member's braces are balanced,
    # so leaving it in place would let _balance_braces conclude all is well.
    if hoist_into is not None:
        text, nested = _split_nested_members(text)
        hoist_into.extend(nested)
    text = _repair_null_conditional_assign(text)
    text = _repair_struct_member_assign(text)
    text = _repair_gameobject_component_assign(text, gameobject_fields)
    text = _repair_bang_count_comparison(text)
    text = _declare_inline_out_params(text)
    text = _repair_numeric_literals(text)
    text = _repair_wrong_namespace_qualifiers(text)
    text = _qualify_ambiguous_unity_types(text)
    text = _repair_static_called_as_instance(text)
    text = _repair_forcemode_on_rigidbody2d(text)
    text = _repair_self_class_static_call(text, self_class_name)
    # Two structural defects that CANNOT be repaired from the fragment alone.
    # Both were live on the grandma run and together took the whole build down
    # (17 of 28 gate errors), because a broken fragment does not fail alone —
    # it consumes the members that follow it:
    #
    #   NpcDialogPlayer.cs(173,118) CS1026 — a call left unclosed:
    #       PlayLine("Petunia", "The air is thick with dread..."
    #   EnemyFSM.cs(100,5) CS8803/CS0106/CS1022 — a surplus `}` closed the
    #       CLASS early, so every member after it parsed at top level.
    #
    # Neither has a ground truth in the text. The missing `)` belongs at the
    # end of ITS OWN statement, not at the end of the fragment, and appending
    # one there just moves the error; guessing per-line is worse still, since
    # `if (x` and `var z = Foo(a` are indistinguishable from a truncated call
    # and "repairing" the first yields a silently empty if-body. Dropping a
    # surplus `}` has the mirror problem — it merges two blocks that were
    # meant to be separate. So reject, the same call `_balance_braces` already
    # makes for over-closed braces, and let the injection point keep its
    # deterministic baseline body. A generic micro still builds; a poisoned
    # one fails the syntax gate and there is no project at all.
    paren_depth = _code_paren_depth(text)
    if paren_depth != 0:
        console.print(f"  [yellow]FIX[/]  rejected {what}: unbalanced parentheses "
                      f"({paren_depth:+d}) — an unclosed call has no repairable position")
        return ""
    if _code_brace_depth(text) < 0:
        console.print(f"  [yellow]FIX[/]  rejected {what}: more '}}' than '{{' — "
                      f"a surplus closer would close the enclosing class early")
        return ""
    text = _balance_braces(text)
    text = _cap_lines(text, what=what)
    return text.strip()


# CS0104 ("'Random' is an ambiguous reference between 'UnityEngine.Random' and
# 'System.Random'"). A generated MonoBehaviour has `using UnityEngine;`, and the
# moment anything adds `using System;` — which the extras legitimately need for
# Action, Math or DateTime — every bare `Random` and `Object` in the file
# becomes ambiguous. Hit live on Chromabreak in three micros at once:
#
#     Vector2 r = Random.insideUnitCircle * patrolRadius;   (behavior_tree)
#     Quaternion.Euler(-_recoilKick, Random.Range(...), 0f) (statemachine)
#     GameObject inst = Object.Instantiate(_prefab, ...);   (objectives)
#
# Scoped to members that exist ONLY on the UnityEngine type, so what the author
# meant is not in question — `System.Random` has no `insideUnitCircle`, and
# `System.Object` has no `Instantiate`. `new Random()` is deliberately NOT
# matched: that one really is the System type, and qualifying it would be the
# wrong repair. This is why `Random` is excluded from `_TYPE_NAMESPACES` —
# adding a using cannot resolve an ambiguity, only qualifying the use can.
_UNITY_RANDOM_MEMBERS = (
    "Range", "value", "insideUnitCircle", "insideUnitSphere", "onUnitSphere",
    "rotation", "rotationUniform", "ColorHSV", "InitState", "state",
)
_UNITY_OBJECT_MEMBERS = (
    "Instantiate", "Destroy", "DestroyImmediate", "DontDestroyOnLoad",
    "FindObjectOfType", "FindObjectsOfType", "FindAnyObjectByType",
    "FindFirstObjectByType", "FindObjectsByType",
)
_AMBIGUOUS_TYPE_RES = tuple(
    (re.compile(r"(?<![\w.])" + name + r"\.(?=(?:" + "|".join(members) + r")\b)"),
     f"UnityEngine.{name}.")
    for name, members in (("Random", _UNITY_RANDOM_MEMBERS),
                          ("Object", _UNITY_OBJECT_MEMBERS))
)


def _qualify_ambiguous_unity_types(text: str) -> str:
    """Qualify `Random.`/`Object.` uses that can only mean the UnityEngine type."""
    for rx, repl in _AMBIGUOUS_TYPE_RES:
        text = rx.sub(repl, text)
    return text


# CS1014 ("A get or set accessor expected"): the model authors a PROPERTY
# through the method channel — a signature with no parameter list, and a body
# that is a bare `return`. Hit live on a grandma run,
# SceneTransition.cs(74):
#
#     public bool IsPlaying
#     {
#         return Time.time >= startTime && ...;      <- needs a get accessor
#     }
#
# The template renders `signature { body }` verbatim, which is valid for a
# method and invalid for a property. Wrapping the body in `get { … }` keeps the
# member the model intended rather than dropping it or forcing parens onto it.
_PROPERTY_SIG_RE = re.compile(
    r"^(?:\[[^\]\n]*\]\s*)*"
    r"(?:public|private|protected|internal)\b"
    r"(?:\s+(?:static|virtual|override|sealed|new|readonly))*"
    r"\s+[A-Za-z_][\w<>,\[\].?]*"          # type
    r"\s+[A-Za-z_]\w*\s*$"                 # name, and nothing after it
)
_ACCESSOR_RE = re.compile(r"^\s*(?:get|set)\b\s*[{;=]", re.MULTILINE)


# A FIELD filed in the method channel. The model writes the declaration as the
# "signature" and the initializer as the "body":
#
#   {"signature": "private Vector3 _anchorPos", "body": "= Vector3.zero;"}
#
# which the template renders as `private Vector3 _anchorPos { = Vector3.zero; }`
# — four CS1014s per occurrence. Hit live on Chromabreak's composed
# `interactable` micro (WorldInteractable.cs), twice in one file.
#
# It is a field, so it is moved to the field channel rather than dropped: the
# author's declaration and initializer both survive, and `lint_fields` then
# applies its full repair pass (reserved-name collisions, `sanitize_default`,
# CS0236 initializers) to it exactly as if it had been declared there.
_FIELD_DECL_SIG_RE = re.compile(
    r"^(?:\[[^\]\n]*\]\s*)*"
    r"(?:(?:public|private|protected|internal|static|readonly|const|new)\s+)*"
    r"(?P<type>[A-Za-z_][\w<>,\[\].?]*)"
    r"\s+(?P<name>[A-Za-z_]\w*)\s*$"
)
_FIELD_INIT_BODY_RE = re.compile(r"^\s*=\s*(?P<value>.+?)\s*;?\s*$", re.S)


def _split_field_shaped_methods(
        fields: list[CodeField],
        methods: list[CodeMethod]) -> tuple[list[CodeField], list[CodeMethod]]:
    """Move `extra_methods` entries that are really field declarations into
    `extra_fields`. Returns (fields, methods) — never raises."""
    moved: list[CodeField] = []
    kept:  list[CodeMethod] = []
    for m in methods:
        sig  = (m.signature or "").strip()
        body = m.body or ""
        sig_m  = _FIELD_DECL_SIG_RE.match(sig) if "(" not in sig else None
        body_m = _FIELD_INIT_BODY_RE.match(body)
        if sig_m and body_m and not _ACCESSOR_RE.search(body):
            try:
                moved.append(CodeField(name=sig_m.group("name"),
                                       type=sig_m.group("type"),
                                       default=body_m.group("value")))
            except Exception:  # noqa: BLE001 — a name/type the field model refuses
                kept.append(m)
                continue
            console.print(
                f"  [magenta]FIX[/]  moved {sig!r} from extra_methods to "
                f"extra_fields — a declaration with an initializer is a field, "
                f"not a method (CS1014)")
            continue
        kept.append(m)
    return list(fields) + moved, kept


def _wrap_property_accessor(sig: str, body: str) -> str:
    """Give a parameterless property body the `get` accessor C# requires."""
    if "(" in sig or not _PROPERTY_SIG_RE.match(sig):
        return body
    if _ACCESSOR_RE.search(body):
        return body                        # already has get/set — leave alone
    if not re.search(r"\breturn\b", body):
        return body                        # not a readable property; gate reports
    console.print("  [magenta]FIX[/]  wrapped property body in a get "
                  "accessor (CS1014)")
    indented = "\n".join(("    " + ln) if ln.strip() else ln
                         for ln in body.splitlines())
    return f"get\n{{\n{indented}\n}}"


# CS0115 ("no suitable method found to override"): every generated MonoBehaviour
# (and ScriptableObject, e.g. AudioBank.cs.j2) derives directly from a Unity
# base class that does NOT declare these — Unity invokes them via reflection
# ("magic methods"), not C# virtual dispatch, so `override` on any of them is a
# hard compile error. Hit live on a RepoFlow `transition` micro run,
# SceneTransition.cs(69):
#
#     public override void OnEnable()
#     {
#         base.OnEnable(); _fadeCanvas = GetComponentInChildren<Canvas>(); ...
#     }
#
# — the model assumed a base-class implementation to specialize. Both the
# modifier and the now-illegal `base.OnEnable()` call (CS0117: 'MonoBehaviour'
# does not contain a definition for 'OnEnable') have to go; the rest of the
# body is exactly the behavior that was wanted.
#
# A curated list of REAL Unity messages is not the whole gap, though: the model
# can just as easily invent a plausible-sounding hook name that is not a Unity
# message either — hit live on a DeadReckoningProtocol `spawner` micro run,
# EnemySpawner.cs(129):
#
#     public override void OnSpawned()
#     { ... configure this spawner's own AudioSource for the prefab it holds ... }
#
# — no base class declares `OnSpawned`, invented or otherwise, so `override` is
# illegal here too (CS0115), for the same structural reason. The general rule
# is therefore inverted from "strip only these known-illegal names": `override`
# is legal ONLY on a name the class's OWN template actually declares `virtual`/
# `abstract` (e.g. Dialogue.cs.j2's `Resolve` — see each pipeline's
# `_KNOWN_VIRTUAL_METHODS`) — everything else gets stripped, Unity message or
# invented alike. `_UNITY_MESSAGE_METHODS` is kept as an explicit, unconditional
# floor: those names are illegal to override REGARDLESS of what any pipeline's
# `known_virtual_methods` happens to list, so a future mistake there can't
# resurrect the SceneTransition bug.
_UNITY_MESSAGE_METHODS = frozenset({
    "Awake", "Start", "OnEnable", "OnDisable", "OnDestroy",
    "OnApplicationQuit", "OnApplicationPause", "OnApplicationFocus",
    "Update", "LateUpdate", "FixedUpdate",
    "OnGUI", "OnDrawGizmos", "OnDrawGizmosSelected", "OnValidate", "Reset",
    "OnTriggerEnter", "OnTriggerStay", "OnTriggerExit",
    "OnTriggerEnter2D", "OnTriggerStay2D", "OnTriggerExit2D",
    "OnCollisionEnter", "OnCollisionStay", "OnCollisionExit",
    "OnCollisionEnter2D", "OnCollisionStay2D", "OnCollisionExit2D",
    "OnMouseDown", "OnMouseUp", "OnMouseEnter", "OnMouseExit", "OnMouseOver",
    "OnMouseDrag", "OnMouseUpAsButton",
    "OnBecameVisible", "OnBecameInvisible",
    "OnTransformChildrenChanged", "OnTransformParentChanged",
})
_OVERRIDE_MODIFIER_RE = re.compile(r"\b(?:override|sealed)\s+")


def _strip_illegal_message_override(sig: str, body: str, name: str, *,
                                    known_virtual_methods: "frozenset[str]" = frozenset()
                                    ) -> tuple[str, str]:
    """Drop `override`/`sealed` from `sig` and any `base.<name>(...)` call from
    `body`, unless `name` is one of the template's own genuine `virtual`/
    `abstract` members (`known_virtual_methods` — always overridable) and not
    itself a hard-illegal Unity message name. No-op when `sig` has no
    `override`/`sealed` at all."""
    if not re.search(r"\boverride\b", sig):
        return sig, body
    if name in known_virtual_methods and name not in _UNITY_MESSAGE_METHODS:
        return sig, body
    new_sig = _OVERRIDE_MODIFIER_RE.sub("", sig)
    base_call_re = re.compile(rf"\bbase\s*\.\s*{re.escape(name)}\s*\([^)]*\)\s*;\s*")
    new_body = base_call_re.sub("", body)
    if name in _UNITY_MESSAGE_METHODS:
        console.print(f"  [magenta]FIX[/]  stripped illegal 'override' + base.{name}() "
                     f"call — Unity message methods are not virtual (CS0115)")
    else:
        console.print(f"  [yellow]FIX[/]  stripped illegal 'override' from {name!r} "
                     f"(CS0115) — no base method by that name exists, so nothing calls "
                     f"it automatically; it is now live only if other authored code "
                     f"calls it directly")
    return new_sig, new_body


def lint_method(m: CodeMethod, *, taken_names: set[str],
                hoist_into: Optional[list[CodeMethod]] = None,
                gameobject_fields: "frozenset[str]" = frozenset(),
                known_virtual_methods: "frozenset[str]" = frozenset(),
                self_class_name: str = "") -> Optional[CodeMethod]:
    """
    Validate + clean a CodeMethod. Returns None if it should be dropped.
    `taken_names` is mutated with the method's lower-cased identifier so
    duplicates are dropped. `hoist_into` collects members the model nested
    inside this method's body — see `lint_code_fragment`. `gameobject_fields`
    and `self_class_name` are forwarded to it too — see
    `_repair_gameobject_component_assign` / `_repair_self_class_static_call`.
    `known_virtual_methods` names this generator's OWN template's genuine
    `virtual`/`abstract` members — see `_strip_illegal_message_override`.
    """
    sig  = m.signature.strip()
    body = lint_code_fragment(m.body, what=f"method {sig!r}", hoist_into=hoist_into,
                              gameobject_fields=gameobject_fields,
                              self_class_name=self_class_name)
    if not body:
        return None
    body = _wrap_property_accessor(sig, body)
    # Containment for everything the wrapper above could not make legal: with no
    # parameter list, the emitted `sig { body }` is a PROPERTY, and a property
    # body without a get/set accessor is CS1014 no matter what is in it. The
    # field-shaped case is recovered upstream (`_split_field_shaped_methods`);
    # whatever still lands here is a member this pass cannot render, and a loud
    # drop beats writing a syntax error into the project.
    if "(" not in sig and not _ACCESSOR_RE.search(body):
        console.print(f"  [yellow]FIX[/]  dropped {sig!r}: no parameter list and "
                      f"no get/set accessor — neither a method nor a property "
                      f"(CS1014)")
        return None
    # Extract method name for dedupe (best-effort; brittle parse is OK here).
    name_match = re.search(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", sig)
    if name_match:
        name = name_match.group(1)
        sig, body = _strip_illegal_message_override(
            sig, body, name, known_virtual_methods=known_virtual_methods)
        if not body.strip():
            console.print(f"  [yellow]FIX[/]  dropped method {name!r}: nothing "
                          f"left after stripping the illegal base call")
            return None
        key = name.lower()
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
    "UnityEngine.Events", "UnityEngine.EventSystems",
    "UnityEngine.SceneManagement", "UnityEngine.Rendering",
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
    # CS0103: `Array.IndexOf`/`Array.Sort`/etc. without `using System;`. Hit
    # live on the RepoQuest run, BranchStrategyController.cs(24) —
    # `int index = Array.IndexOf(_strategies.ToArray(), _currentStrategy);`
    # with only `System.Collections.Generic`/`System.Linq` imported. Matched
    # on the static-call shape so the common English word "array" in an
    # authored comment or string is never mistaken for the type.
    (r"\bArray\.(?:IndexOf|LastIndexOf|Sort|Reverse|Find|FindAll|FindIndex|"
     r"FindLast|FindLastIndex|Exists|ForEach|Resize|Copy|Clear|ConvertAll|"
     r"BinarySearch|TrueForAll|Empty|CreateInstance)\s*\(", "System"),
    # UnityAction<T> is the same namespace as UnityEvent and just as easy for a
    # model to reach for on a `SerializeField` callback field without the
    # using -- hit live, PauseMenu.cs(18,110) CS0246 on a bare `UnityAction`.
    (r"\bUnityEvent\b|\bUnityAction\b",                     "UnityEngine.Events"),
    (r"\bSceneManager\s*\.",                               "UnityEngine.SceneManagement"),
    (r"\bTMP_\w+|\bTextMeshPro",                           "TMPro"),
    (r"\bIEnumerator\b",                                   "System.Collections"),
    (r"\bSpriteAtlas\b",                                   "UnityEngine.U2D"),
    (r"\b(?:Tilemap|TileBase)\b",                          "UnityEngine.Tilemaps"),
    (r"\bPlayableDirector\b",                               "UnityEngine.Playables"),
    (r"\bVideoPlayer\b",                                    "UnityEngine.Video"),
    (r"\bTask\s*[<(]|\bTask\.(?:Run|Delay|WhenAll|WhenAny)\b", "System.Threading.Tasks"),
    # LINQ. Hit live on the CodeVault run, FirstCommitObjectives.cs(102,78):
    #
    #     _fileIcons = FindObjectsOfType<GameObject>().Where(g => …).ToArray();
    #
    # CS1061, and the error message says "are you missing a using directive"
    # in as many words. These are EXTENSION methods, so the symptom is not an
    # unknown name but a known type that suddenly has no such member — which is
    # why the unbound-identifier pass cannot see it either. Matched only as
    # `.Method(`, on the operators that are unambiguously LINQ: a bare `Select`
    # or `Count` is ordinary English and appears in authored comments.
    (r"\.(?:Where|Select|SelectMany|OrderBy|OrderByDescending|ThenBy|FirstOrDefault|"
     r"LastOrDefault|SingleOrDefault|ToArray|ToList|ToDictionary|Any|All|Sum|Average|"
     r"Distinct|Concat|Except|Intersect|GroupBy|Zip|Take|Skip|TakeWhile|SkipWhile)\s*\(",
     "System.Linq"),
    # `.Count(` is excluded from the list above for exactly the reason its own
    # comment gives (`Count` reads as ordinary English), EXCEPT when a lambda
    # follows inside the parens — `list.Count(x => …)` is unambiguously the
    # LINQ predicate overload; the plain property (`list.Count`) never takes an
    # argument at all. Hit live, GameRules.cs(282) CS1955 "Non-invocable member
    # 'List<GameRules.Step>.Count' cannot be used like a method" — the model
    # reached for `steps.Count(s => s.objectiveId == "obj_2")` with no
    # `System.Linq` imported, so the compiler resolved `.Count` as the
    # property instead of the extension method.
    (r"\.Count\s*\([^)]*=>", "System.Linq"),
    # `catch (Exception e)` / `catch (ArgumentException ex)` / a bare
    # `throw new SomeException(...)` — every BCL exception type lives in
    # `System`, and `_KNOWN_SYMBOLS` already treats them all as legitimate
    # names (so `bind_authored_code` never flags one as unbound), but nothing
    # ever added the import those names actually need. The known-symbol check
    # answers "is this a real type" (CS0103); Unity's compiler still asks
    # "which assembly" (CS0246) — a different question this table exists to
    # answer. Hit live, SaveLoadManager.cs(34) CS0246: "The type or namespace
    # name 'Exception' could not be found" with `using System.IO;` and
    # `using UnityEngine.UI;` and nothing from bare `System`.
    (r"\bcatch\s*\(\s*\w*Exception\b|\bnew\s+\w*Exception\s*\(", "System"),
    # uGUI. Hit live on the TowerControl run, SceneTrigger.cs(28,106):
    #
    #     [SerializeField] private Button _clearancePad = null;
    #
    # with the trigger template's own usings and nothing else — CS0246. The
    # gap is old (nothing here ever mapped a uGUI type) but it was rare while
    # every game had an avatar: a walk controller's authored fields are
    # transforms and rigidbodies. A UI-first game's are Buttons, and the
    # micro-code step is asked for exactly those, so this went from a corner
    # to the common case the moment a board could compile.
    #
    # `com.unity.ugui` backs both namespaces and is always in a composed
    # manifest — hud and menu are unconditional core subsystems and both
    # declare it (MicroComposer._KIND_MODULES). This heal only ever runs on
    # micro schemas, so that guarantee covers every file it can touch.
    (r"\b(?:Button|Text|Image|RawImage|Slider|Scrollbar|ScrollRect|Toggle|"
     r"ToggleGroup|Dropdown|InputField|Selectable|GraphicRaycaster|"
     r"CanvasScaler|ContentSizeFitter|AspectRatioFitter|LayoutElement|"
     r"(?:Horizontal|Vertical|Grid)LayoutGroup)\b",   "UnityEngine.UI"),
    # The pointer's own event types. A drag-and-drop board is the shape of
    # game that reaches for these, and OnPointerClick(PointerEventData) is
    # the signature a model writes without being asked.
    (r"\b(?:PointerEventData|EventSystem|EventTrigger|BaseEventData|"
     r"StandaloneInputModule|IPointer(?:Click|Enter|Exit|Down|Up)Handler|"
     r"I(?:Begin|End)?DragHandler|IDropHandler|IScrollHandler)\b",
     "UnityEngine.EventSystems"),
]


# Namespaces an authored body needs but never asks for.
#
# lint_usings below only ever FILTERS the model's `extra_usings`; nothing has
# ever ADDED one. So a body that calls `Array.Fill(...)` — perfectly good C# —
# ships without `using System;` and fails with CS0103 "The name 'Array' does
# not exist in the current context", which reads like an invented symbol rather
# than a missing import. Hit live in Gridfall's GridLogic row-clear.
#
# Deliberately a SMALL, unambiguous table. Each entry is a type whose namespace
# has exactly one sane answer in a Unity script. `Random` is excluded on
# purpose: `UnityEngine.Random` and `System.Random` are both real, and adding
# `using System;` to a body that means the Unity one introduces CS0104
# ambiguity — a repair that trades one compile error for another.
_TYPE_NAMESPACES: "dict[str, str]" = {
    "Array": "System",
    "Math": "System",
    "Convert": "System",
    "DateTime": "System",
    "TimeSpan": "System",
    "StringComparison": "System",
    "IDisposable": "System",
    "List": "System.Collections.Generic",
    "Dictionary": "System.Collections.Generic",
    "HashSet": "System.Collections.Generic",
    "Queue": "System.Collections.Generic",
    "Stack": "System.Collections.Generic",
    "IEnumerator": "System.Collections",
    # UI Toolkit. Only the types with ONE sane answer: `Button`, `Label`,
    # `Image` and `Text` all exist in BOTH UnityEngine.UI and
    # UnityEngine.UIElements, so importing either for those is the CS0104
    # ambiguity `Random` is excluded for. These four have no uGUI twin.
    "VisualElement": "UnityEngine.UIElements",
    "UIDocument": "UnityEngine.UIElements",
    "VisualTreeAsset": "UnityEngine.UIElements",
    "StyleSheet": "UnityEngine.UIElements",
    # New Input System device singletons. A model writes `Mouse.current.delta`
    # for mouse-look whatever `input_backend` the variant declares, and the
    # legacy-backend controller template imports nothing for it: eight CS0103
    # "The name 'Mouse' does not exist" in one PlayerController.cs, live on
    # Chromabreak. Adding the import is the faithful repair — the code already
    # names these types, so the alternative is not "legacy input", it is a
    # controller that does not compile.
    "Mouse": "UnityEngine.InputSystem",
    "Keyboard": "UnityEngine.InputSystem",
    "Gamepad": "UnityEngine.InputSystem",
    "Touchscreen": "UnityEngine.InputSystem",
    "InputSystem": "UnityEngine.InputSystem",
}

# Every identifier inside a declared type — `List<VisualElement>[]` names two.
_TYPE_TOKEN_RE = re.compile(r"[A-Za-z_]\w*")


def usings_for_declared_types(types) -> "list[str]":
    """Namespaces the DECLARED TYPES of authored fields need, in first-seen order.

    `required_usings_for` reads BODIES, where a type appears as `Name.`,
    `Name<` or `Name(` — the three shapes `_TYPE_USE_RES` matches. A field
    declaration has none of them: the type is followed by the field's own name.
    So a micro that declared `[SerializeField] private VisualElement _pauseTint`
    imported nothing for it and shipped CS0246 "The type or namespace name
    'VisualElement' could not be found". Hit live on Chromabreak's composed
    `menu` micro, which is a uGUI menu the model gave a UI Toolkit field.
    """
    out: "list[str]" = []
    for raw in types:
        for token in _TYPE_TOKEN_RE.findall(str(raw or "")):
            ns = _TYPE_NAMESPACES.get(token)
            if ns and ns not in out:
                out.append(ns)
    return out

# `Array.Fill(` / `new List<` / `List<int> x` — a real reference, not the word
# appearing inside an identifier or a string.
_TYPE_USE_RES: "dict[str, re.Pattern[str]]" = {
    name: re.compile(r"(?<![\w.])" + re.escape(name)
                     + r"(?=\s*[.<(])")
    for name in _TYPE_NAMESPACES
}


def _rank(type_name: str) -> int:
    """How many `[]` suffixes a jagged-array type carries. `int[][]` -> 2."""
    return str(type_name or "").count("[]")


def jagged_rank_corrections(fields: "list[tuple[str, str]]", body: str
                            ) -> "dict[str, str]":
    """`{field_name: corrected_type}` where the author's own code contradicts
    the rank they declared for a jagged array.

    Only ONE signal is trusted, and it is the author stating the type
    themselves: an explicit local declaration initialised straight from the
    field —

        [SerializeField] private int[][][] _grid;   // declared rank 3
        ...
        int[][] grid = _grid;                       // used as rank 2, x5

    That is the author disagreeing with the author, in writing, and the local
    is the side they went on to index. Every other kind of rank mismatch is
    left alone: inferring a type from indexing depth guesses, and a guess that
    lowers a rank correctly used elsewhere trades a compile error for a wrong
    program. Hit live in Gridfall's GridLogic — one bad declaration produced
    seven CS0029s across five methods.
    """
    out: "dict[str, str]" = {}
    if not body:
        return out
    for name, declared in fields:
        if _rank(declared) < 2 or not name:
            continue
        base = declared.split("[")[0].strip()
        rx = re.compile(re.escape(base) + r"((?:\[\])+)\s+\w+\s*=\s*"
                        + re.escape(name) + r"\b")
        ranks = {len(m.group(1)) // 2 for m in rx.finditer(body)}
        ranks = {r for r in ranks if 0 < r < _rank(declared)}
        if len(ranks) == 1:
            out[name] = base + "[]" * ranks.pop()
    return out


# Types a model routinely qualifies with the WRONG namespace. `Touchscreen`,
# `Gamepad` and friends read like core engine types, so an author writing a
# mobile input script reaches for `UnityEngine.Touchscreen` — CS0234, "does not
# exist in the namespace 'UnityEngine'". They live in UnityEngine.InputSystem.
# Live on BlockWell's TouchInput, which had already imported the right
# namespace and then over-qualified the type anyway.
_MISQUALIFIED_TYPES: "dict[str, str]" = {
    name: "UnityEngine.InputSystem" for name in (
        "Touchscreen", "Keyboard", "Mouse", "Gamepad", "Pointer", "Pen",
        "InputAction", "InputActionAsset", "InputActionMap", "InputControl",
        "InputDevice", "InputSystem", "TouchPhase",
    )
}


def correct_qualified_type(type_str: str) -> "tuple[str, str]":
    """`UnityEngine.Touchscreen` -> `("Touchscreen", "UnityEngine.InputSystem")`.

    Returns `(type, required_namespace)`; the namespace is "" when nothing was
    corrected. Only rewrites a qualifier we can name a correct home for — an
    unknown `Foo.Bar` is left exactly as written.
    """
    raw = (type_str or "").strip()
    if "." not in raw:
        return raw, ""
    head, _, tail = raw.rpartition(".")
    ns = _MISQUALIFIED_TYPES.get(tail)
    if ns and head != ns:
        return tail, ns
    return raw, ""


def required_usings_for(text: str) -> "list[str]":
    """Namespaces `text` uses but may not have imported, in declaration order."""
    if not text:
        return []
    out: "list[str]" = []
    for name, rx in _TYPE_USE_RES.items():
        if rx.search(text):
            ns = _TYPE_NAMESPACES[name]
            if ns not in out:
                out.append(ns)
    return out


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


# A bare if/else-if/while header whose condition references a bad identifier
# can't be commented out, but its condition CAN always be swapped for the
# literal `false` -- syntactically valid regardless of what the original
# expression was, and always disables the guarded block without touching any
# other line's braces. Restricted to if/while: for/foreach/switch conditions
# aren't simple boolean swaps, so those are left alone (skipped) for the
# compiler to catch.
#
# The opening `{` may sit on this line or a later one, and either way it is
# PRESERVED verbatim -- disabling a condition must never change brace
# structure. Hit live on a TurboToys run, RivalPathfinderBT.cs(132,13):
#
#     if (playerPositionPercent < 0.45f) { // Trigger taunt audio here }
#
# a header, its brace, and a trailing comment that swallowed the closer, all on
# one physical line. Without the `{`/comment tail the pattern did not match, so
# the line was skipped entirely and its CS0103 survived to the build.
_DISABLEABLE_HEADER_RE = re.compile(
    r'^(?P<indent>\s*)(?P<prefix>(?:\}\s*)?(?:else\s+)?(?:if|while))\s*'
    r'\((?P<cond>.*)\)\s*(?P<brace>\{)?[ \t]*(?://.*)?$')


def _disable_bad_header(line: str, names: "list[str] | set[str]") -> "str | None":
    """Return `line` with an if/else-if/while condition replaced by `false`,
    or None if `line` isn't a header of that shape (e.g. has a trailing `;`, or
    is a for/foreach/switch -- those are left for the caller to handle via the
    normal comment-out / skip path).

    A trailing `{` is re-emitted, and a trailing `//` comment is dropped along
    with the condition it belonged to -- the comment cannot carry a real closing
    brace (anything inside `//` was never a delimiter), so dropping it leaves the
    brace count exactly as it was.
    """
    m = _DISABLEABLE_HEADER_RE.match(line)
    if not m:
        return None
    # `cond`'s `.*` is greedy: given a NESTED `if (...) { ... }` on the same
    # physical line (own header + brace + trailing comment, same shape as the
    # TurboToys case above but one level deeper), it walks past the outer
    # header's own `)` and swallows the inner if's header AND brace into
    # `cond`, then re-emits only ONE `{` for what were really two real opens.
    # Both original closes survive untouched further down the file, so the
    # rewrite quietly drops one open brace -- an extra `}` overflows the
    # method/class scope (CS8803/CS0106/CS1022). Confirmed live: EnemySpawner
    # .cs's OnGuardDetected/OnVehicleDetected, both
    # `if (x.CompareTag(...)) { ...; if (y) { // comment } }`. A genuine
    # if/while condition expression never contains a raw brace, so `cond`
    # holding one is proof the match walked into a nested structure it can't
    # safely collapse to `false` -- decline, matching every other case this
    # function already treats as "neither repair is safe: leave the line
    # alone for the compiler to report."
    if "{" in m.group("cond") or "}" in m.group("cond"):
        return None
    brace = f" {m.group('brace')}" if m.group("brace") else ""
    return (f"{m.group('indent')}{m.group('prefix')} (false){brace} "
            f"/* AUDIT: condition removed, referenced invalid "
            f"{', '.join(sorted(set(names)))} */")


def _neutralize_lines(text: str, get_bad_names, make_comment) -> tuple[str, set, set]:
    """Shared line-oriented neutralization walk used by lint_hash_references,
    lint_member_references, and unity_micro_code._neutralize_refs -- all
    three comment out (or disable the condition of) any physical line that
    references a name which turned out to be invalid. Centralized so the one
    cross-line invariant they all depend on is enforced exactly once: a bare
    (brace-less) `if`/`while` whose only embedded statement IS the very next
    line must not be left with no statement at all when that next line also
    gets blanked to a comment. Confirmed live: an `if (_ref != null)` header
    with brace-less body `_ref.Foo();` on the next line -- both referencing
    the same dropped field -- were neutralized independently, leaving
    `if (false) /* AUDIT */` followed only by a comment (CS1525/CS1002).

    `get_bad_names(line_index, line)` returns the bad names referenced on
    that physical line (falsy if none). `make_comment(names, line)` returns
    the full `// AUDIT: ...` replacement text for a line that gets blanked.
    Returns (text, flagged_names, skipped_names).
    """
    out: list[str] = []
    flagged: set = set()
    skipped: set = set()
    pending_bare_body = False
    for i, line in enumerate(text.splitlines()):
        if line.lstrip().startswith("//"):
            out.append(line)
            pending_bare_body = False
            continue
        names = get_bad_names(i, line)
        if not names:
            out.append(line)
            pending_bare_body = False
            continue
        if not _safe_to_comment_out(line):
            disabled = _disable_bad_header(line, names)
            if disabled is not None:
                flagged.update(names)
                out.append(disabled)
                m = _DISABLEABLE_HEADER_RE.match(line)
                assert m is not None  # _disable_bad_header already matched this same line
                pending_bare_body = not m.group("brace")
                continue
            skipped.update(names)
            out.append(line)
            pending_bare_body = False
            continue
        flagged.update(names)
        indent = line[: len(line) - len(line.lstrip())]
        prefix = "; " if pending_bare_body else ""
        out.append(f"{indent}{prefix}{make_comment(names, line)}")
        pending_bare_body = False
    return "\n".join(out), flagged, skipped


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

    def get_bad_names(_i: int, line: str) -> list[str]:
        return [n for n in _HASH_REF_RE.findall(line) if n not in allowed]

    def make_comment(names: list, line: str) -> str:
        return (f"// AUDIT: removed invalid Hash member(s) "
                f"{', '.join(sorted(set(names)))} -- {line.strip()}")

    result, flagged, skipped = _neutralize_lines(text, get_bad_names, make_comment)
    if flagged:
        console.print(f"  [yellow]FIX[/]  removed statement(s) referencing "
                      f"invalid Hash member(s): {', '.join(sorted(flagged))}")
    if skipped:
        console.print(f"  [yellow]WARN[/] could not safely remove invalid Hash "
                      f"member(s) on a control-flow or brace-mixed line: "
                      f"{', '.join(sorted(skipped))}")
    return result


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

    def make_comment(names: list, line: str) -> str:
        return (f"// AUDIT: removed invalid member reference(s) "
                f"{', '.join(sorted(set(names)))} -- {line.strip()}")

    result, flagged, skipped = _neutralize_lines(
        text, lambda i, _line: bad_lines.get(i), make_comment)
    if flagged:
        console.print(f"  [yellow]FIX[/]  removed statement(s) referencing "
                      f"invalid member(s): {', '.join(sorted(flagged))}")
    if skipped:
        console.print(f"  [yellow]WARN[/] could not safely remove invalid member "
                      f"reference(s) on a control-flow or brace-mixed line: "
                      f"{', '.join(sorted(skipped))}")
    return result


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


# CS-invisible — Function.Call(Hash.X, ...) passing the wrong number of
# arguments. InputArgument[] is variadic, so csc compiles it clean regardless
# of count; the mismatch only shows up as a runtime SHVDN3 exception. Checked
# against ground truth mined from real decompiled Rockstar mission scripts
# (tools/mine_native_arity.py -> lookups/gta_native_arity.json via
# gta_allowlists.load_native_arity), restricted to natives with a single,
# unambiguous observed argument count — see that loader's docstring.
def find_function_call_arity_mismatches(
    source: str, *, arity: "dict[str, int] | None"
) -> list[tuple[int, str, int, int]]:
    """Return [(line_number, native, expected, actual), ...] for every
    Function.Call(Hash.X, ...) whose argument count doesn't match X's known,
    unambiguous arity. `arity=None` (or an unknown/ambiguous native) is a
    no-op. Only fires when Hash.X is exactly the call's first argument —
    never on a nested Function.Call passed as someone else's argument, and
    never on the qualified `GTA.Native.Hash.X` form (not used in LLM-authored
    extras, which rely on the template's `using GTA.Native;`)."""
    if not source or not arity:
        return []
    out: list[tuple[int, str, int, int]] = []
    for m in _FUNCTION_CALL_ARGS_RE.finditer(source):
        parts = _split_top_level_args(m.group(1))
        head = parts[0].strip()
        hm = _HASH_REF_RE.fullmatch(head)
        if not hm:
            continue
        native = hm.group(1)
        expected = arity.get(native)
        if expected is None:
            continue
        actual = len(parts) - 1
        if actual != expected:
            out.append((source.count("\n", 0, m.start()), native, expected, actual))
    return out


def lint_function_call_arity(text: str, *, arity: "dict[str, int] | None") -> str:
    """Neutralize a Function.Call(Hash.X, ...) statement whose argument count
    doesn't match X's known, unambiguous arity. Same comment-out-or-disable-
    header strategy as the other lint_* passes here."""
    if not text or not arity:
        return text
    bad = find_function_call_arity_mismatches(text, arity=arity)
    if not bad:
        return text

    bad_lines: dict[int, list[str]] = {}
    for line_no, native, expected, actual in bad:
        bad_lines.setdefault(line_no, []).append(
            f"{native} (expected {expected} arg(s), got {actual})")

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
        out.append(f"{indent}// AUDIT: removed Function.Call with wrong argument "
                   f"count -- {', '.join(sorted(set(names)))} -- {line.strip()}")
    if flagged:
        console.print(f"  [yellow]FIX[/]  removed statement(s) calling a native "
                      f"with the wrong argument count: {', '.join(sorted(flagged))}")
    if skipped:
        console.print("  [yellow]WARN[/] could not safely remove a Function.Call "
                      "arity mismatch on a control-flow or brace-mixed line")
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

# Valid C# numeric literal suffixes, lower-cased — the same set the
# fragment-level `_repair_numeric_literals` works from, plus "" for a bare
# literal (an initializer may legitimately carry no suffix, a `12f` inside a
# statement is matched only when one is present). Derived rather than restated
# so the two passes can never disagree about what C# accepts.
_CS_NUM_SUFFIXES  = frozenset(_VALID_NUM_SUFFIXES) | frozenset({""})
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

# CS1002/CS1010/CS0246: a collection-typed field's only valid initializers are
# "null" and a `new ...` construction — anything else (a bare comma list of
# words, prose) is not an expression a collection type can hold at all, unlike
# the numeric/bool/string branches below there is no repair that does not
# invent content, so this is a structural gate rather than a fixer. Hit live
# on the RepoQuest run, BranchStrategyController.cs(11):
#
#     [SerializeField] private List<string> _strategies = GitHubFlow", "GitFlow;
#
# — the model meant `new List<string> { "GitHubFlow", "GitFlow" }` and instead
# emitted the bare comma list. `_initializer_is_structurally_safe` alone let
# this through: its quotes are balanced (2) and it opens no bracket, so it
# read as a legal-looking "arbitrary expression" while starting with a bare
# unquoted identifier that is not one.
_COLLECTION_TYPE_RE = re.compile(
    r"^(?:list|dictionary|hashset|queue|stack|linkedlist|"
    r"ienumerable|icollection|ilist|iset|idictionary|readonlycollection)"
    r"\s*<|\[\s*\]$")

# A hex colour is the one notation every model reaches for and the one notation
# C# cannot hold: `Color c = "#FFD700"` is CS0029 however it is quoted, and
# `'#FFD700'` is CS1012 on top. The declared type makes the intent unambiguous,
# so this is a repair rather than a drop — the field keeps the colour the model
# chose. #RGB / #RGBA shorthand expands the way CSS does (nibble x 17).
_COLOR_TYPES = frozenset({"color", "color32"})
_HEX_COLOR_RE = re.compile(
    r"^#?(?P<hex>[0-9a-fA-F]{3}|[0-9a-fA-F]{4}"
    r"|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
# A genuine colour expression the model may also write: `Color.white`,
# `new Color32(255, 215, 0, 255)`. Kept as authored — nothing here can improve it.
_COLOR_EXPR_RE = re.compile(r"^(?:new\s+Color(?:32)?\s*\(|Color(?:32)?\s*\.)")


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


# CS1012 ("Too many characters in character literal"): a C# `'…'` holds exactly
# ONE character, but a model reaching for a string reaches for whichever quote
# it feels like. Hit live on a TurboToys run, ImpactVFX.cs(23,148):
#
#     [SerializeField] private Color _colorPalette = '#FFD700';
#
# `_code_mask` masks a char literal WHOLE, by design — it answers "is this
# position code?", and the characters inside a literal are not. So every
# structural check downstream looked straight past this one: no newline, no
# unbalanced bracket, no stray `;` at a code position, and `_unescaped_quote_count`
# counts only `"`. The initializer sailed through as an "expression" and was
# spliced verbatim.
#
# Exactly one character, or one escape sequence C# actually defines.
_VALID_CHAR_LITERAL_RE = re.compile(
    r"^(?:[^'\\\n]"                        # one ordinary character
    r"|\\['\"\\0abfnrtv]"                  # simple escape
    r"|\\x[0-9a-fA-F]{1,4}"                # hex escape
    r"|\\u[0-9a-fA-F]{4}"                  # UTF-16 escape
    r"|\\U[0-9a-fA-F]{8})$")               # UTF-32 escape


def _has_malformed_char_literal(text: str) -> bool:
    """True when `text` contains a `'…'` that is not a legal char literal.

    Finds the literals through `_code_mask` rather than a second scanner: a
    masked run that BEGINS where code left off is a literal's opening quote, so
    the runs starting with `'` are the char literals — string bodies, comments
    and the `'` inside `"it's"` are already masked away by the same pass every
    other check here trusts.
    """
    mask = _code_mask(text)
    n = len(text)
    i = 0
    while i < n:
        if mask[i]:
            i += 1
            continue
        start = i
        while i < n and not mask[i]:
            i += 1
        run = text[start:i]
        if not run.startswith("'"):
            continue                                   # a string or a comment
        if len(run) < 2 or not run.endswith("'"):
            return True                                # never terminated
        if not _VALID_CHAR_LITERAL_RE.match(run[1:-1]):
            return True
    return False


# CS1001/CS1002/CS1026/CS8803 — a JSON/JS-style object literal (`{x: 0f, y:
# 0f, z: 0f}`) spliced in as a value. No C# expression opens a `{` block with
# a bare `identifier :` inside it — object/collection initializers use `=`,
# never `:` — so the shape is decisive on its own. Unlike every check in
# `_initializer_is_structurally_safe` below, this one is NOT about balance:
# the brace and the paren wrapping it are both perfectly matched, which is
# exactly why this shape used to sail through that function as "safe". Hit
# live, DropZone's SaveSystem.cs(18):
#
#     [SerializeField] private Vector3 _formationPos
#         = new Vector3({x: 0f, y: 0f, z: 0f});
#
# — the model's own field default was the raw string
# `"new Vector3({x: 0f, y: 0f, z: 0f})"`, a JSON object where positional
# constructor args belong. Scanned at CODE positions only (`_code_mask`), so
# a format-specifier hole in an interpolated string (`$"{value:F2}"`, already
# masked whole by that pass) can never trip this.
_OBJECT_LITERAL_RE = re.compile(r"\{\s*[A-Za-z_]\w*\s*:")


def _has_object_literal_shape(text: str) -> bool:
    """True when `text` contains a `{ identifier : ...}` object/dict literal
    at a CODE position — see `_OBJECT_LITERAL_RE` above."""
    mask = _code_mask(text)
    masked = "".join(ch if mask[i] else "#" for i, ch in enumerate(text))
    return bool(_OBJECT_LITERAL_RE.search(masked))


def _initializer_is_structurally_safe(text: str) -> bool:
    """True when `text` can stand as `= <text>;` without corrupting the file.

    The declaration is emitted on ONE line, so a newline, a stray `;` or an
    unclosed bracket does not merely mistype this field — it swallows or splits
    every member after it. Brackets and semicolons are judged at CODE positions
    only (`_code_mask`), so a `;` inside `"a;b"` is not held against it. A
    JSON/JS object literal (`_has_object_literal_shape`) is rejected even
    though its brackets balance — see that check's own comment.
    """
    if "\n" in text or "\r" in text:
        return False
    if _unescaped_quote_count(text) % 2:
        return False                                   # unterminated literal
    if _has_malformed_char_literal(text):
        return False
    if _has_object_literal_shape(text):
        return False
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


# CS0236 ("A field initializer cannot reference the non-static field, method,
# or property 'X'"): C# evaluates field initializers BEFORE the constructor
# body, so `this` does not exist yet — ANY reference to instance state from an
# initializer is a hard compile error, however well-formed the expression is.
# Hit live on a Monaco monobike run, MonobikeController.cs(9,45) and
# MonobikeAI.cs(9,53):
#
#     [SerializeField] private Rigidbody    rb      = GetComponent<Rigidbody>();
#     [SerializeField] private NavMeshAgent aiAgent = GetComponent<NavMeshAgent>();
#
# This is the model reaching for the idiom it knows from Awake() and writing it
# one line too early. `sanitize_default` cannot see it: the expression is
# perfectly well-FORMED — one line, balanced, no stray `;` — so every
# structural check passes and the text reaches the file verbatim.
#
# Unlike the numeric-literal repairs, this one has somewhere to go, so it is a
# MOVE rather than a drop: the author's intent is `rb = GetComponent<Rigidbody>();`
# inside Awake(), where the same expression is legal and does exactly what was
# meant. Dropping the initializer also compiles, but leaves a [SerializeField]
# nothing ever assigns — the guarded body (`if (rb != null) …`) then silently
# does nothing forever, which is the wrong-silent-fallback this codebase
# rejects. Same rationale as the CS0106 hoist above. A render site with no
# Awake() channel still gets the drop (a null field beats a dead assembly) plus
# a loud log naming the remedy.
#
# Matched only where the name is NOT preceded by `.` — a receiver-qualified
# call such as `Camera.main.GetComponent<T>()` or `Camera.main.transform` never
# touches `this` and is perfectly legal in an initializer.
_INSTANCE_MEMBERS = (
    # Component / GameObject lookups
    "GetComponent", "GetComponents", "GetComponentInChildren",
    "GetComponentInParent", "GetComponentsInChildren", "GetComponentsInParent",
    "TryGetComponent", "AddComponent", "GetInstanceID", "CompareTag",
    # MonoBehaviour lifecycle helpers
    "StartCoroutine", "StopCoroutine", "Invoke", "InvokeRepeating",
    "CancelInvoke", "SendMessage", "SendMessageUpwards", "BroadcastMessage",
    # Object / Component / Behaviour instance properties
    "transform", "gameObject", "tag", "name", "hideFlags",
    "enabled", "isActiveAndEnabled", "useGUILayout",
    # The keywords themselves
    "this", "base",
)
# Longest-first so `GetComponentInChildren` is tried before `GetComponent`.
_INSTANCE_REF_RE = re.compile(
    r"(?<![\w.])(?:"
    + "|".join(sorted(_INSTANCE_MEMBERS, key=len, reverse=True))
    + r")\b")


def _unqualified_name_re(names: "set[str]") -> "re.Pattern[str]":
    return re.compile(
        r"(?<![\w.])(?:"
        + "|".join(sorted((re.escape(n) for n in names), key=len, reverse=True))
        + r")\b")


def initializer_instance_refs(text: str, *, siblings: "set[str]") -> list[str]:
    """Names that make `text` a CS0236 field initializer, in order of appearance.

    Two sources, both unambiguous: instance members of Component/MonoBehaviour
    used without a receiver, and the class's OWN fields — one extra_field's
    initializer reading another (or itself) is the same error. Judged at CODE
    positions only (`_code_mask`), so `= "transform"` and a `// name` comment
    are not held against it.
    """
    if not text:
        return []
    mask = _code_mask(text)
    hits: list[tuple[int, str]] = [
        (m.start(), m.group(0))
        for m in _INSTANCE_REF_RE.finditer(text) if mask[m.start()]
    ]
    if siblings:
        hits += [
            (m.start(), m.group(0))
            for m in _unqualified_name_re(siblings).finditer(text) if mask[m.start()]
        ]
    out: list[str] = []
    for _, name in sorted(hits):
        if name not in out:
            out.append(name)
    return out


def _unquote(text: str) -> str:
    """Strip one matching pair of surrounding quotes, either kind — the model's
    choice of quote says nothing about what it meant."""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1].strip()
    return text


def _hex_rgba(digits: str) -> tuple[int, int, int, int]:
    """#RGB / #RGBA / #RRGGBB / #RRGGBBAA -> four 0-255 components."""
    if len(digits) in (3, 4):
        parts = [int(d, 16) * 17 for d in digits]
    else:
        parts = [int(digits[i:i + 2], 16) for i in range(0, len(digits), 2)]
    while len(parts) < 4:
        parts.append(255)
    return parts[0], parts[1], parts[2], parts[3]


def _cs_float(value: float) -> str:
    """A C# float literal — 4 decimal places is well inside float precision and
    keeps the generated line readable."""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return (text or "0") + "f"


# CS1503: Color32's constructor takes `byte`, which has no implicit conversion
# from `float` (unlike Color32->Color widening, which IS implicit — that
# direction is fine whatever the declared field type is). `_COLOR_EXPR_RE`
# above accepts an already-Color32-shaped authored expression uncritically;
# this catches the one way that shape still fails to compile. Hit live on a
# RepoFlow custom script, TutorialIcon.cs(10): `new Color32(0, 1f, 1f, 255)`
# mixed byte-range ints with normalized-float components in the same call —
# there is no single intended value to recover, so this drops rather than
# guesses, same as every other branch here.
_FLOAT_LITERAL_ARG_RE = re.compile(r"^[+-]?(?:\d+\.\d+|\.\d+|\d+[fF])$")


def _color32_args_are_bytes(text: str) -> bool:
    """False when a `Color32(...)` / `new Color32(...)` call's arguments
    include a float literal. True for everything else (plain-int args,
    `Color32.member`, `Color(...)`) — this only ever rejects the one broken
    pattern, never a call it cannot fully parse."""
    m = re.match(r"^(?:new\s+)?Color32\s*\((.*)\)$", text.strip(), re.DOTALL)
    if not m:
        return True
    args = _split_top_level_args(m.group(1))
    return not any(_FLOAT_LITERAL_ARG_RE.match(a.strip()) for a in args)


def _color_initializer(base: str, text: str) -> Optional[str]:
    """A Color/Color32 initializer, or None to drop it.

    Colour is the one declared type where the model's habitual notation is not
    C# at all, so the type is doing real work here: `#FFD700` on a `Color` field
    can only mean one thing. Anything that is neither a hex colour nor a colour
    expression (a bare `gold`, a stray number) has no compilable reading and is
    dropped rather than spliced.
    """
    hexed = _HEX_COLOR_RE.match(_unquote(text))
    if hexed is None:
        if (_COLOR_EXPR_RE.match(text) and _initializer_is_structurally_safe(text)
                and _color32_args_are_bytes(text)):
            return text
        return None
    r, g, b, a = _hex_rgba(hexed.group("hex"))
    if base == "color32":
        return f"new Color32({r}, {g}, {b}, {a})"
    return ("new Color(" + ", ".join(_cs_float(c / 255) for c in (r, g, b, a)) + ")")


_VECTOR_ARITY = {"vector2": 2, "vector3": 3, "vector4": 4}
_VECTOR_CTOR  = {"vector2": "Vector2", "vector3": "Vector3", "vector4": "Vector4"}
# A bare tuple literal is valid C# (a ValueTuple) but has no implicit
# conversion to UnityEngine.Vector2/3/4 (CS0029) — the model's habitual
# shorthand for "three numbers", confirmed live, PlayerCamera.cs(14,141):
#     [SerializeField] private Vector3 _offset = (0f, 2f, 0f);
_TUPLE_LITERAL_RE = re.compile(r"^\((?P<items>[^()]+)\)$")


def _vector_initializer(base: str, text: str) -> Optional[str]:
    """A Vector2/3/4 initializer, or None to drop it.

    A tuple literal whose arity matches the declared type is rewritten to the
    real constructor call; anything else is already an expression (`new
    Vector3(...)`, `Vector3.zero`) and only the structural guard applies.

    A bare word (`null`, a stray identifier) is rejected outright rather than
    reaching that structural guard — unlike `_color_initializer`, which
    already refuses this shape, the guard alone only checks brace/paren/quote
    BALANCE, which "null" trivially passes despite being CS0037 for a
    non-nullable value type. Confirmed live (2026-08-28),
    LightingDirector.cs(48): `[SerializeField] private Vector3 _skyboxColor
    = null;` shipped unrepaired for exactly this reason."""
    if re.fullmatch(r"[A-Za-z_]\w*", text) and text not in ("true", "false"):
        return None
    m = _TUPLE_LITERAL_RE.match(text)
    if m:
        items = [p.strip() for p in _split_top_level_args(m.group("items"))]
        if (len(items) == _VECTOR_ARITY[base] and all(items)
                and all(map(_initializer_is_structurally_safe, items))):
            return f"new {_VECTOR_CTOR[base]}({', '.join(items)})"
        return None
    # Bare comma-separated numbers with NO wrapping parens at all — a second
    # habitual shorthand alongside the tuple-literal one above, confirmed
    # live, DamageReceiver.cs(11,142):
    #     [SerializeField] private Vector3 _knockbackForce = 0f, 0f, 0f;
    # C# reads that as three separate field declarators, not one initializer
    # (CS1001/CS1003) — the tuple branch above never sees it because there are
    # no parens to match, and it would otherwise reach the structural-safety
    # fallback below, which only checks brace/paren/quote balance (trivially
    # fine for a string with none of those) and ships it unrepaired. A real
    # Vector expression never has a top-level comma outside a constructor
    # call, so finding one here is decisive: matching arity rewrites to the
    # real constructor exactly like the tuple case, any other arity is
    # dropped rather than falling through to a check that cannot see this.
    # `_split_top_level_args` tracks `(`/`[` depth only, not `{` — so a JSON
    # object literal with no wrapping parens at all (`{x: 0f, y: 0f, z: 0f}`)
    # also lands here, splitting on its commas into arity-matching pieces
    # that are each individually unbalanced (`{x: 0f`, ` z: 0f}`). Requiring
    # every piece to pass the SAME structural-safety check applied below
    # catches that: an unmatched brace fails it, so this can't misfire as a
    # confident "repair" that is still the original defect wearing new commas.
    items = _split_top_level_args(text)
    if len(items) > 1:
        items = [p.strip() for p in items]
        if (len(items) == _VECTOR_ARITY[base] and all(items)
                and all(map(_initializer_is_structurally_safe, items))):
            return f"new {_VECTOR_CTOR[base]}({', '.join(items)})"
        return None
    return text if _initializer_is_structurally_safe(text) else None


# Bracket-literal rebuild only — deliberately NOT named _VECTOR_ARITY, which
# already exists above with a narrower key set and a matching _VECTOR_CTOR.
# Shadowing it routed a `Color` field into _vector_initializer (the dispatch at
# `base in _VECTOR_ARITY`) where _VECTOR_CTOR has no "color" key: KeyError, and
# wrong anyway since Color has its own _color_initializer.
_BRACKET_ARITY = {"vector2": 2, "vector3": 3, "vector4": 4, "quaternion": 4,
                  "color": 4, "color32": 4}


def _as_float_literal(tok: str) -> str:
    """`0.5` -> `0.5f`; an int or an already-suffixed literal is left alone.

    A Vector component is a float, so `new Vector2(0.5, 0.5)` is CS1503 —
    fixing the brackets without fixing the literal just moves the error.
    """
    if re.fullmatch(r"-?\d+\.\d+", tok):
        return tok + "f"
    return tok


def _rebuild_bracket_literal(type_str: str, parts: "list[str]") -> str:
    """`Vector2` + ["0.5","0.5"] -> `new Vector2(0.5f, 0.5f)`; `int[]` + [...]
    -> `new int[] { ... }`. "" when the shape is not one we can name."""
    if not parts:
        return ""
    raw  = type_str.strip().rstrip("?").strip()
    base = raw.lower()
    if base.endswith("[]"):
        elem = raw[:-2]
        toks = ([_as_float_literal(p) for p in parts]
                if elem.strip().lower() == "float" else parts)
        return f"new {elem}[] {{ {', '.join(toks)} }}"
    arity = _BRACKET_ARITY.get(base)
    if arity is not None and len(parts) == arity:
        return f"new {raw}({', '.join(_as_float_literal(p) for p in parts)})"
    return ""


# A SCALAR field handed a comma-separated list of literals. The declaration
# renders as `= <default>;`, so the model's own words come out as
#
#     [SerializeField] private string _introLines = "Welcome…","Survive…","Don't blink.";
#
# — three CS1001/CS1003 per field, and the whole build stopped at the syntax
# gate (live: Chromabreak's QuartermasterBrain.cs, four fields in a row). What
# was meant is not in doubt: several values of one type is an ARRAY, so the
# field is promoted to `<type>[]` and the values wrapped in an array
# initializer. That keeps every line the model wrote, which dropping all but
# the first would not.
#
# Scoped to a list whose items are all the same KIND of literal, and only for a
# type that is not already a collection — an expression list with a `(` or `{`
# in it could be one `new Foo(a, b)` call, where the commas are arguments.
_LIST_ITEM_SPLIT_RE = re.compile(r'"(?:[^"\\]|\\.)*"|[^,]+')


def _promote_list_default_to_array(field) -> bool:
    """Retype a scalar field whose default is a literal list to an array."""
    default = (getattr(field, "default", None) or "").strip()
    base = (getattr(field, "type", "") or "").strip()
    if ("," not in default or not base
            or "[" in base or "<" in base
            or _COLLECTION_TYPE_RE.search(base.lower())
            or any(ch in default for ch in "({[")):
        return False
    items = [m.group(0).strip() for m in _LIST_ITEM_SPLIT_RE.finditer(default)]
    items = [i for i in items if i]
    if len(items) < 2:
        return False
    quoted = [i.startswith('"') and i.endswith('"') and len(i) > 1 for i in items]
    if not (all(quoted) or not any(quoted)):
        return False                      # mixed shapes — not one list
    if all(quoted) and base.lower() not in ("string", "system.string"):
        return False                      # string literals into a non-string field
    field.type = f"{base}[]"
    field.default = "new " + base + "[] { " + ", ".join(items) + " }"
    console.print(f"  [magenta]FIX[/]  retyped `{base} {field.name}` to "
                  f"`{base}[]` — its initializer is a {len(items)}-item list "
                  f"(CS1003)")
    return True


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

    # A bracketed collection literal — Python/JS syntax that is not C#:
    #
    #     [SerializeField] private Vector2 _targetRect = [0.5, 0.5];
    #
    # CS1525 "Invalid expression term '['". Live on Gridfall's CameraController.
    # Same contract as the unquoting repair below: the model meant the VALUE,
    # the syntax is the only thing wrong, so rebuild it rather than drop it.
    # Anything whose shape we cannot name confidently still drops.
    if text.startswith("[") and text.endswith("]"):
        parts = [p.strip() for p in text[1:-1].split(",") if p.strip()]
        rebuilt = _rebuild_bracket_literal(type_str, parts)
        return rebuilt if rebuilt else None

    if base == "string":
        if text.lower() in _STRING_SENTINELS:
            return text if _initializer_is_structurally_safe(text) else None
        if text.startswith(_STRING_LITERAL_PREFIXES):
            # A well-formed `"..."` literal's own content can never hold an
            # unescaped quote — one would close the token early. When it
            # does, the outer pair isn't a literal delimiter at all: it's an
            # extra layer the model wrapped around an initializer that was
            # already valid by itself (and may itself embed a string
            # literal, e.g. `Application.dataPath + "/SaveData.json"`). The
            # overall quote COUNT stays even either way (4, not 2), so the
            # plain balance check below can't tell the two shapes apart —
            # peel the outer layer first and judge what's left instead. Hit
            # live, SaveLoadManager.cs(11,112): CS1003.
            if (text.startswith('"') and text.endswith('"') and len(text) > 1
                    and _unescaped_quote_count(text[1:-1])):
                inner = text[1:-1]
                return inner if _initializer_is_structurally_safe(inner) else None
            return text if _initializer_is_structurally_safe(text) else None
        # Bare prose the model forgot to quote (CS0103) — quoting keeps the
        # authored value; dropping it would lose the content for no reason.
        return f'"{escape_cs_string(text)}"'

    # CS0029: a STRING literal initialising a field that is not a string. Hit
    # live on a TurboToys run, SaveSystem.cs(15,166):
    #
    #     [SerializeField] private Dictionary<string, object> _saveData = "null";
    #
    # — the model quoted the null sentinel, and every branch below is numeric or
    # bool, so the quoted text fell through to the structural guard untouched and
    # shipped. `object`/`var`/`dynamic` are excepted: there a string genuinely
    # does convert.
    #
    # Unquoting is a REPAIR, not a drop: a model that quotes a value
    # (`float _drag = "0.5"`, `bool _loop = "true"`) meant the value, and the
    # quotes are the only thing wrong. The recursive call then re-judges the
    # unquoted text against the declared type exactly as if it had arrived that
    # way. `_unquote` strips the quote, so this recurses at most once.
    if (text.startswith(_STRING_LITERAL_PREFIXES)
            and base not in ("object", "var", "dynamic")):
        inner = _unquote(text)
        if inner == text:                              # `@"…"` / `$"…"` — no pair
            return None
        # A bare word is not an initializer for a non-string type. Accepting it
        # would trade CS0029 for CS0103, which is not a repair.
        if (re.fullmatch(r"[A-Za-z_]\w*", inner)
                and inner not in ("null", "true", "false", "default")):
            return None
        return sanitize_default(type_str, inner)

    if base == "bool":
        if text in ("true", "false"):
            return text
        if text in ("1", "0"):                         # CS0029 — int is not bool
            return "true" if text == "1" else "false"
        return None

    if _COLLECTION_TYPE_RE.search(base):
        if text == "null":
            return text
        if text.startswith("new") and _initializer_is_structurally_safe(text):
            return text
        return None

    if base.rsplit(".", 1)[-1] in _COLOR_TYPES:
        return _color_initializer(base.rsplit(".", 1)[-1], text)

    if base.rsplit(".", 1)[-1] in _VECTOR_ARITY:
        return _vector_initializer(base.rsplit(".", 1)[-1], text)

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


def lint_fields(fields: list[CodeField], *, reserved: set[str],
                hoist_into: Optional[list[str]] = None) -> list[CodeField]:
    """Drop fields whose name collides with template-emitted names or duplicates,
    and repair initializers that would not parse (see `sanitize_default`).

    `hoist_into`, when given, receives `name = <expr>;` statements for
    initializers that reference instance state (CS0236) so the caller can
    re-home them into Awake(). Callers whose template has no Awake() injection
    point omit it and the initializer is dropped instead — see
    `initializer_instance_refs`.
    """
    out: list[CodeField] = []
    seen: set[str] = set()
    sibling_names = {f.name for f in fields}
    for f in fields:
        key = f.name.lower()
        if key in reserved or key in seen:
            console.print(f"  [yellow]FIX[/]  dropped duplicate/reserved field {f.name!r}")
            continue
        seen.add(key)
        # `default` is interpolated raw into the declaration, so anything wrong
        # with it is a compile error the syntax gate can only report after the
        # fact (`private int _seed = -1i;` and `= 0i;`, both live).
        #
        # `sanitize_default` rather than `_repair_numeric_literals` here: it
        # subsumes the suffix strip for this position (`-1i` -> `-1`) and adds
        # what only a DECLARATION can know — the declared type. That type is
        # what turns `0.5` into CS0664 on a float field and `2.5` into CS0266 on
        # an int one, and it is unavailable to the fragment-level pass, which
        # sees loose statements with no declaration in view.
        # `_repair_numeric_literals` keeps its own job on fragment bodies.
        _promote_list_default_to_array(f)
        fixed = sanitize_default(f.type, f.default)
        if fixed != f.default:
            if fixed is None:
                console.print(f"  [yellow]FIX[/]  dropped uncompilable initializer "
                              f"{f.default!r} on `{f.type} {f.name}`")
            else:
                console.print(f"  [magenta]FIX[/]  repaired initializer on "
                              f"`{f.type} {f.name}`: {f.default!r} -> {fixed!r}")
        # A well-FORMED initializer can still be illegal: `this` does not exist
        # when field initializers run, so an instance reference is CS0236 no
        # matter how it parses. Move it to Awake() where it belongs.
        if fixed is not None:
            refs = initializer_instance_refs(fixed, siblings=sibling_names)
            if refs:
                named = ", ".join(refs)
                if hoist_into is not None:
                    hoist_into.append(f"{f.name} = {fixed};")
                    console.print(f"  [magenta]FIX[/]  moved instance-referencing "
                                  f"initializer on `{f.type} {f.name}` into Awake() "
                                  f"(CS0236: {named})")
                else:
                    console.print(f"  [yellow]FIX[/]  dropped instance-referencing "
                                  f"initializer on `{f.type} {f.name}` (CS0236: "
                                  f"{named}) — this render site has no Awake() "
                                  f"channel; author the assignment in awake_extra")
                fixed = None
        if fixed != f.default:
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

    # ── Schema Extensions (LLM field invention sandbox) ──────────────────────
    # See docs/SCHEMA_EXTENSIONS.md / CLAUDE.md "Schema Extensions" — the ONLY
    # place the Pipeline LLM may invent fields outside the fixed schema surface
    # above. Sandboxed by `apply_extensions_safe_block`, NOT `lint_extensible`:
    # no pruning, no clamping, no renaming — a name collision is a loud
    # abstain (see that function), never a silent fix.
    extensions_fields:   list[CodeField]  = Field(default_factory=list)
    extensions_methods:  list[CodeMethod] = Field(default_factory=list)
    extensions_metadata: dict             = Field(default_factory=dict)


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
                    template_usings: set[str],
                    hoist_field_inits: bool = False,
                    known_virtual_methods: "frozenset[str]" = frozenset(),
                    template_field_types: "dict[str, str] | None" = None,
                    template_readonly_names: "frozenset[str]" = frozenset()) -> _ExtensibleLike:
    """
    Run the full defensive lint over every `extra_*` field of an extensible
    schema, in place. Mirrors the controller's hand-written pass so every
    generator gets identical, battle-tested cleaning for free.

    `hoist_field_inits` is the caller's assertion that its template actually
    RENDERS `awake_extra` — only then can a CS0236 initializer be re-homed
    there instead of dropped. It is opt-in rather than default because eleven
    micro templates render extra_fields without an Awake() injection point
    (ai, animator, dialogue, interactable, lighting, shader, statemachine,
    transition, trigger, vfx), and hoisting into a channel nothing reads would
    lose the assignment silently — worse than the loud drop.

    `known_virtual_methods` names this generator's OWN template's genuine
    `virtual`/`abstract` members (e.g. Dialogue.cs.j2's `Resolve`) — an
    authored `override` of one of these is legitimate and left alone; every
    other `override` is illegal (CS0115, no base method exists — see
    `_strip_illegal_message_override`) and gets stripped. Defaults to empty:
    a generator whose template declares no virtuals (every one of them today
    except dialogue) needs no override to ever survive.

    `template_field_types` (typically `template_field_types(template_dir,
    variant, facts)`, `code_fields.py`'s own reader for a micro's `.cs.j2`
    field declarations) folds the TEMPLATE's own fields into the same misuse
    check that already covers the schema's own `extra_fields` — see the
    comment at `own_field_types` below for why this was missing and what it
    caught live. Defaults to None (no template fields known): a caller that
    never reads its template still gets exactly today's behavior.

    `template_readonly_names` (typically `template_readonly_property_names(
    template_dir, variant, facts)`) names the template's get-only,
    expression-bodied properties whose body is too complex for
    `_repair_readonly_property_assign` to rewrite an assignment into (a
    simple `Name => _field;` passthrough still gets that value-preserving
    rewrite via `bind_authored_code`'s own path for `type: custom` scripts,
    but `lint_extensible` never had it either — see
    `find_readonly_property_misuse`). An assignment into one of these is
    refused outright (CS0200) rather than left broken. Defaults to empty.
    """
    schema.extra_usings = lint_usings(schema.extra_usings, already_in_template=template_usings,
                                      allowed=_KNOWN_NAMESPACES)
    # BEFORE lint_fields: a field the model filed as a method has to reach the
    # field channel while that channel's own repair pass can still run on it.
    schema.extra_fields, schema.extra_methods = _split_field_shaped_methods(
        schema.extra_fields, schema.extra_methods)
    field_inits: list[str] = []
    schema.extra_fields = lint_fields(
        schema.extra_fields, reserved=reserved_fields,
        hoist_into=field_inits if hoist_field_inits else None)
    # AFTER lint_fields, so a dropped field cannot pull in a namespace nothing
    # declares any more. Appended rather than run back through `lint_usings`:
    # these come from `_TYPE_NAMESPACES`, a curated table, not from the model.
    for _ns in usings_for_declared_types(f.type for f in schema.extra_fields):
        if _ns not in template_usings and _ns not in schema.extra_usings:
            schema.extra_usings.append(_ns)
            console.print(f"  [magenta]FIX[/]  added `using {_ns};` — an "
                          f"extra_field's declared type needs it (CS0246)")
    # For _repair_gameobject_component_assign — computed AFTER lint_fields so a
    # field it dropped is never treated as a real GameObject-typed target.
    gameobject_fields = frozenset(
        f.name for f in schema.extra_fields
        if (f.type or "").strip() == "GameObject" and f.name)
    # Members the model nested inside a body (CS0106) are re-homed here, at the
    # one layer that can see both the bodies and the class-level method list.
    # getattr, not schema.name: _ExtensibleLike's structural contract doesn't
    # promise a `.name` (UnityScript/CodeExtensibleSchema both happen to have
    # one, but nothing enforces that every future satisfier will) — an empty
    # self_class_name just makes _repair_self_class_static_call a no-op.
    self_class_name = getattr(schema, "name", "") or ""
    hoisted: list[CodeMethod] = []
    schema.awake_extra        = lint_code_fragment(schema.awake_extra,        what="Awake() body",         hoist_into=hoisted, gameobject_fields=gameobject_fields, self_class_name=self_class_name)
    # AFTER the fragment lint, and FIRST in the body: these assignments are
    # already valid C# (they must not be re-judged as authored prose), and the
    # author's own Awake() statements may read the fields they initialise.
    if field_inits:
        schema.awake_extra = "\n".join(
            field_inits + ([schema.awake_extra] if schema.awake_extra else []))
    schema.update_extra       = lint_code_fragment(schema.update_extra,       what="per-frame body",       hoist_into=hoisted, gameobject_fields=gameobject_fields, self_class_name=self_class_name)
    schema.fixed_update_extra = lint_code_fragment(schema.fixed_update_extra, what="FixedUpdate() body",   hoist_into=hoisted, gameobject_fields=gameobject_fields, self_class_name=self_class_name)
    taken: set[str] = set()
    cleaned: list[CodeMethod] = []
    # A queue, not a for-loop: a hoisted method can itself contain a nested
    # member, and a hoisted name must still be deduped against the authored
    # ones. Bounded so a pathological fragment cannot spin.
    queue: list[CodeMethod] = list(schema.extra_methods) + hoisted
    budget = len(queue) + _MAX_HOISTED_METHODS
    while queue and budget > 0:
        budget -= 1
        nested: list[CodeMethod] = []
        m2 = lint_method(queue.pop(0), taken_names=taken, hoist_into=nested,
                         gameobject_fields=gameobject_fields,
                         known_virtual_methods=known_virtual_methods,
                         self_class_name=self_class_name)
        if m2 is not None:
            cleaned.append(m2)
        queue.extend(nested)
    schema.extra_methods = cleaned

    # AFTER every body is in its final linted form — this reads the actual
    # call sites, not an intermediate draft of them.
    schema.extra_fields = retype_string_fields_used_as_audioclip(
        schema.extra_fields, blob=_authored_blob(schema))

    # Self-field-type misuse: `lint_extensible` originally had no cross-check
    # against a TEMPLATE's field types the way `bind_authored_code` does for
    # `type: custom` scripts — only the SCHEMA'S OWN extra_fields were ground
    # truth here. Confirmed live, StealthAudioManager.cs(20,38): two
    # `object`-typed extra_fields (cross-script references with no sanctioned
    # real type — see docs/model-pipeline-architecture.md) had real-sounding
    # methods called on them (`RetractGear`, `OnEscortComplete`), which can
    # never exist on `object`.
    #
    # That left a second, later gap: misuse of the TEMPLATE's OWN pre-existing
    # fields went uncaught by this pass, because it never saw their types
    # either (AnimatorPipeline hit the SAME gap independently — see
    # micro/animator.py's own default_fix — but predates lint_extensible
    # entirely, so it calls lint_field_type_misuse directly rather than
    # through here). Confirmed live via THIS path, DeadReckoningProtocol
    # GameRules.cs(265,277): `_objectives.Count`/`_objectives[i]` — `_objectives`
    # is Rules.cs.j2's own `private MonoBehaviour _objectives;`, invisible
    # here until a caller passes `template_field_types`. Extra_fields still
    # win a name collision (the author's own explicit declaration is the
    # more specific fact).
    own_field_types = {**(template_field_types or {}),
                       **{f.name: f.type for f in schema.extra_fields if f.name}}
    if own_field_types:
        schema.awake_extra, _ = lint_field_type_misuse(
            schema.awake_extra, field_types=own_field_types)
        schema.update_extra, _ = lint_field_type_misuse(
            schema.update_extra, field_types=own_field_types)
        schema.fixed_update_extra, _ = lint_field_type_misuse(
            schema.fixed_update_extra, field_types=own_field_types)
        schema.extra_methods = [
            m.model_copy(update={"body": lint_field_type_misuse(
                m.body, field_types=own_field_types, signature=m.signature)[0]})
            for m in schema.extra_methods
        ]
        # Same field_types source, several sibling misuse shapes
        # (CS0019/CS1503/CS0029, not the CS1061/CS1503-on-string the pass
        # above catches) — see each repair's own docstring. awake_extra/
        # update_extra/fixed_update_extra are always void, so
        # _repair_struct_return_type_mismatch (needs a non-void `signature`
        # to have anything to check against) only runs over extra_methods,
        # where LLM-authored non-void helper methods actually occur.
        for _repair in (_repair_collection_compared_as_scalar,
                        _repair_animator_play_layer_arg,
                        _repair_getcomponent_wrong_dimension):
            schema.awake_extra        = _repair(schema.awake_extra,        own_field_types)
            schema.update_extra       = _repair(schema.update_extra,       own_field_types)
            schema.fixed_update_extra = _repair(schema.fixed_update_extra, own_field_types)
            schema.extra_methods = [
                m.model_copy(update={"body": _repair(m.body, own_field_types)})
                for m in schema.extra_methods
            ]
        schema.extra_methods = [
            m.model_copy(update={"body": _repair_struct_return_type_mismatch(
                m.body, own_field_types, signature=m.signature)})
            for m in schema.extra_methods
        ]

    # Same discovery, one property over: `CurrentStep += 1;` against
    # Rules.cs.j2's ternary-bodied `CurrentStep` (see
    # `find_readonly_property_misuse`'s own comment for the full citation).
    if template_readonly_names:
        schema.awake_extra, _ = lint_readonly_property_misuse(
            schema.awake_extra, readonly_names=template_readonly_names)
        schema.update_extra, _ = lint_readonly_property_misuse(
            schema.update_extra, readonly_names=template_readonly_names)
        schema.fixed_update_extra, _ = lint_readonly_property_misuse(
            schema.fixed_update_extra, readonly_names=template_readonly_names)
        schema.extra_methods = [
            m.model_copy(update={"body": lint_readonly_property_misuse(
                m.body, readonly_names=template_readonly_names,
                signature=m.signature)[0]})
            for m in schema.extra_methods
        ]

    # This caller has no fixer-log channel of its own, so it prints. The other
    # caller (`harden_against_template`) returns its messages to a pipeline that
    # logs them, and printing here too would double every line.
    for msg in heal_missing_usings(schema, template_usings=template_usings):
        console.print(f"  [magenta]FIX[/]  {msg}")
    return schema


class _ExtensionsLike(Protocol):
    """Structural shape apply_extensions_safe_block needs. CodeExtensibleSchema
    satisfies this nominally (see its extensions_* fields above); a schema
    that hand-rolls extra_* the way ControllerSchema does must hand-roll
    these three too rather than inherit them."""
    extensions_fields:   list[CodeField]
    extensions_methods:  list[CodeMethod]
    extensions_metadata: dict
    meta:                dict


def apply_extensions_safe_block(schema: _ExtensionsLike, *,
                                reserved: set[str]) -> _ExtensionsLike:
    """
    Sandbox `extensions_fields`/`extensions_methods` — see
    docs/SCHEMA_EXTENSIONS.md. Deliberately NOT `lint_extensible`'s repair
    pass: this is the one place in the schema the Pipeline LLM may invent a
    field, and the whole point of that sandbox is that it is NOT run through
    the same defensive machinery as extra_*:
      - no pruning of malformed content
      - no clamping/repair of default values or bodies — a default that
        parses is never rewritten, no matter how odd (see
        test_no_clamping_unlike_lint_fields)
      - no silent auto-rename to dodge a name collision

    Two things this function DOES do, both loud abstains rather than a silent
    drop or a guessed rewrite:

    1. Collision detection — an unexcluded collision would silently corrupt
       the build (CS0102 duplicate member) rather than just fail to compile
       the author's own intent. A name colliding with `reserved`, another
       schema field, a sibling extra_field/extra_method, or another entry in
       extensions_fields/extensions_methods itself excludes the WHOLE entry.
    2. Default-syntax rejection — a default that cannot possibly be valid C#
       (`_initializer_is_structurally_safe`, the same check `sanitize_default`
       uses for extra_fields) has no "intent" left to preserve by shipping it
       verbatim: it would only take the entire compose down at the final
       syntax gate (CSharpSyntaxError aborts the WHOLE project, not just this
       field — see CLAUDE.md "never raise in a way that discards an otherwise
       valid schema"). Only the default is cleared; the field itself (name,
       type, header, tooltip) survives un-renamed and un-rewritten, same as
       collision handling keeps everything BUT the colliding entry. This is
       narrower than `sanitize_default`: it only ever REJECTS, never rewrites
       a parseable-but-questionable value (`"2i"` on an int field still ships
       untouched — that is a lint_fields job, not this one's).

    Both cases are recorded by name in `schema.meta["extensions_conflicts"]`
    — never just dropped quietly.
    """
    known: set[str] = {n.lower() for n in reserved}
    # `model_fields` is pydantic's, not part of `_ExtensionsLike` — every
    # real caller is a BaseModel, but the Protocol deliberately does not
    # promise a pydantic-only attribute, so read it defensively.
    known |= {n.lower() for n in getattr(type(schema), "model_fields", {})}
    known |= {f.name.lower() for f in getattr(schema, "extra_fields", [])}
    known |= {name.lower() for m in getattr(schema, "extra_methods", [])
             if (name := _method_name(m.signature))}

    def _abstain(what: str, name: str) -> None:
        console.print(f"  [red]ABSTAIN[/]  {what} {name!r} collides with a "
                      f"reserved/existing name — excluded from output, not "
                      f"renamed (see extensions_conflicts)")
        schema.meta.setdefault("extensions_conflicts", []).append(
            f"{what} {name!r} collides with an existing name")

    def _abstain_default(name: str, default: str) -> None:
        console.print(f"  [red]ABSTAIN[/]  extensions_fields {name!r} default "
                      f"{default!r} is not valid C# — default dropped, field "
                      f"kept un-initialized (see extensions_conflicts)")
        schema.meta.setdefault("extensions_conflicts", []).append(
            f"extensions_fields {name!r} default {default!r} is not valid C#")

    kept_fields: list[CodeField] = []
    seen: set[str] = set()
    for f in schema.extensions_fields:
        key = f.name.lower()
        if key in known or key in seen:
            _abstain("extensions_fields", f.name)
            continue
        if f.default and not _initializer_is_structurally_safe(f.default):
            _abstain_default(f.name, f.default)
            f.default = None
        seen.add(key)
        kept_fields.append(f)
    schema.extensions_fields = kept_fields

    kept_methods: list[CodeMethod] = []
    seen_methods = set(seen)   # a method sharing a kept field's name collides too
    for m in schema.extensions_methods:
        name = _method_name(m.signature)
        key = name.lower()
        if not key:
            # No parseable name to sandbox against — signature normalization
            # already ran in CodeMethod's own validator; leave it alone
            # rather than guess.
            kept_methods.append(m)
            continue
        if key in known or key in seen_methods:
            _abstain("extensions_methods", name)
            continue
        seen_methods.add(key)
        kept_methods.append(m)
    schema.extensions_methods = kept_methods

    return schema


def neutralize_unavailable_names(schema: _ExtensibleLike, names: "set[str]") -> None:
    """Comment out (or disable the condition of) any line in awake_extra/
    update_extra/fixed_update_extra/extra_methods that references one of
    `names` — template fields that exist for SOME configuration of this
    micro but not the one `schema` actually resolved to (e.g. UI Toolkit's
    `document`/`_root` are declared only when `backend == "UIToolkit"`; a
    variant's own uGUI refs only when it's the active variant).

    `lint_extensible`'s `reserved_fields` only ever prevented the LLM from
    DECLARING a new field with a colliding name — it was never a "these
    names already exist and are safe to read" allowlist, and nothing
    upstream validates extra_* content against template-declared fields at
    all (see unity_micro_code.py's `_neutralize_refs` docstring: the
    per-micro reserved-name sets are known too incomplete to support a
    general undeclared-identifier scan). This closes that gap only where a
    caller can hand over an exact, backend/variant-resolved name set —
    never a guess.
    """
    if not names:
        return
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b")

    def get_bad_names(_i: int, line: str) -> list[str]:
        return pattern.findall(line)

    def make_comment(_names: list, line: str) -> str:
        return f"// AUDIT: removed reference to unavailable field -- {line.strip()}"

    schema.awake_extra, flagged1, _ = _neutralize_lines(schema.awake_extra, get_bad_names, make_comment)
    schema.update_extra, flagged2, _ = _neutralize_lines(schema.update_extra, get_bad_names, make_comment)
    schema.fixed_update_extra, flagged3, _ = _neutralize_lines(schema.fixed_update_extra, get_bad_names, make_comment)
    flagged = flagged1 | flagged2 | flagged3
    for m in schema.extra_methods:
        m.body, f4, _ = _neutralize_lines(m.body, get_bad_names, make_comment)
        flagged |= f4
    if flagged:
        console.print(f"  [yellow]FIX[/]  removed statement(s) referencing "
                      f"field(s) not declared for this backend/variant: "
                      f"{', '.join(sorted(flagged))}")


def _authored_blob(schema) -> str:
    """Everything the model authored into a schema, as one searchable string."""
    return "\n".join(
        [str(getattr(schema, key, "") or "")
         for key in ("awake_extra", "update_extra", "fixed_update_extra")]
        + [f"{f.type} {f.default or ''}"
           for f in (getattr(schema, "extra_fields", None) or [])]
        + [f"{f.type} {f.default or ''}"
           for f in (getattr(schema, "data_fields", None) or [])]
        + [f"{m.signature}\n{m.body}"
           for m in (getattr(schema, "extra_methods", None) or [])]
    )


# CS1503 — a field the model typed `string` (its habitual notation for "the
# name of an asset") passed where an AudioClip is required. There is no
# built-in "load an AudioClip by name" convention in this codebase, so the
# only ground-truth fix is the field's OWN declared type: retype it to
# AudioClip (default -> "null", assignable from the Inspector) rather than
# guess at a Resources.Load path this project never establishes. Confirmed
# live, AudioDirectorAudioManager.cs(59):
#     [SerializeField] private string _exhaustClip = "Exhaust_Heavy";
#     ...
#     _engineSource.PlayOneShot(_exhaustClip, VolumeFor("Engine"));
_AUDIOCLIP_ARG_CALLS = ("PlayOneShot", "PlayClipAtPoint")
_AUDIOCLIP_ARG_RE = re.compile(
    r"\b(?:" + "|".join(_AUDIOCLIP_ARG_CALLS) + r")\s*\(\s*(?P<name>[A-Za-z_]\w*)\b")


def retype_string_fields_used_as_audioclip(fields: list[CodeField], *,
                                           blob: str) -> list[CodeField]:
    """Retype a `string` extra_field to `AudioClip` when the authored blob
    passes it as the clip argument to a known AudioSource call — see above.
    """
    if not fields or not blob:
        return fields
    mask = _code_mask(blob)
    wanted: set[str] = set()
    for m in _AUDIOCLIP_ARG_RE.finditer(blob):
        if mask[m.start()]:
            wanted.add(m.group("name"))
    if not wanted:
        return fields
    out: list[CodeField] = []
    for f in fields:
        if f.name in wanted and (f.type or "").strip().lower() == "string":
            console.print(f"  [magenta]FIX[/]  retyped `{f.name}` from string to "
                          f"AudioClip — passed to PlayOneShot/PlayClipAtPoint "
                          f"(CS1503); assign the clip via the Inspector")
            f = f.model_copy(update={"type": "AudioClip", "default": "null"})
        out.append(f)
    return out


def heal_missing_usings(schema, *, template_usings: "set[str] | frozenset[str]") -> list[str]:
    """Add any namespace the authored content needs and the model forgot.

    Extracted from `lint_extensible` because eight generators (animator,
    controller, lighting, saveload, shader, statemachine, trigger, vfx)
    hand-roll their own lint pass and call `lint_usings` + `lint_fields`
    directly, so they never reached this step. Hit live on a TurboToys run,
    SceneTrigger.cs(20,124):

        [SerializeField] private List<AudioSource> _boostPads = null;

    with the trigger template's own `using UnityEngine; using System.Collections;`
    and nothing else — CS0246 twice over. The heal itself was already written
    and already correct; it simply was not on that code path.
    """
    usings = getattr(schema, "extra_usings", None)
    if usings is None:
        return []
    blob = _authored_blob(schema)
    msgs: list[str] = []
    for pattern, ns in _USING_TRIGGERS:
        if ns in usings or ns in template_usings:
            continue
        if re.search(pattern, blob):
            usings.append(ns)
            msgs.append(f"auto-added missing using {ns!r}")
    return msgs


# ── Template ground truth ─────────────────────────────────────────────────────
#
# CS0102 / CS0111: an LLM-authored member that redeclares one the micro's own
# template already emits. Hit live on a TurboToys run, PlayerHealth.cs:
#
#     (90,18) CS0102 — 'PlayerHealth' already contains a definition for 'OnHealthChanged'
#     (84,17) CS0111 — already defines a member 'TakeDamage' with the same parameter types
#
# The template declares `public event Action<float,float> OnHealthChanged;` and
# `public void TakeDamage(float)`; the model authored both again as extra_methods.
# Nothing checked: `lint_fields` takes a `reserved=` set, but every generator
# hand-authors it and health's lists its FIELDS only — no method names at all —
# while `lint_method` dedupes authored methods against each other and has no
# notion of the template whatsoever. C# puts fields, events and methods in ONE
# member namespace, so the two need one shared set, and hand-maintaining twenty
# of them against twenty templates is how they drift out of date in the first
# place.
#
# So the set is DERIVED from the template, which is the ground truth for what
# the class already contains. Jinja expressions collapse to a placeholder first
# (`= {{ "%.1f"|format(s.max_health) }}f` must not hide `maxHealth`), and type
# declarations are skipped so `public class X : MonoBehaviour` does not reserve
# its base type. A member behind a `{% if %}` stays reserved even when that
# branch is off — over-reserving costs one dropped authored member and says so;
# under-reserving costs the assembly.
_JINJA_EXPR_RE = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_JINJA_TAG_RE  = re.compile(r"\{%.*?%\}|\{#.*?#\}", re.DOTALL)
_TYPE_DECL_RE  = re.compile(r"\b(?:class|struct|interface|enum)\b")
_TEMPLATE_MEMBER_RE = re.compile(
    r"^[ \t]*(?:\[[^\]\n]*\][ \t]*)*"          # attributes
    r"(?:public|private|protected|internal)\b"  # a member always has one here
    r"[^;{(=\n]*?"                              # modifiers + return/field type
    r"\b([A-Za-z_]\w*)[ \t]*"                   # the declared name
    r"(?:\(|;|=>|=[^=]|\{|$)")                  # method, field, property, event
_TEMPLATE_USING_RE = re.compile(r"^[ \t]*using[ \t]+([\w.]+)[ \t]*;", re.MULTILINE)
_TEMPLATE_TYPE_RE = re.compile(r"\b(?:class|struct|interface|enum)\s+([A-Za-z_]\w*)")
# A method DECLARATION line, captured with its parameter list so the arity of a
# call against it can be checked. Same anchor as _TEMPLATE_MEMBER_RE (a member
# always carries an access modifier in these templates), narrowed to the shape
# that has a parameter list.
_TEMPLATE_METHOD_DECL_RE = re.compile(
    r"^[ \t]*(?:\[[^\]\n]*\][ \t]*)*"
    r"(?:public|private|protected|internal)\b"
    r"[^;{(=\n]*?"
    r"\b(?P<name>[A-Za-z_]\w*)[ \t]*\((?P<params>[^)]*)\)")
# An EVENT (or delegate field) declaration, captured with its generic arguments
# so a call against it can be arity-checked exactly like a method.
#
#     public event Action<string> OnObjectiveComplete;
#
# is invoked, not called — and an authored `OnObjectiveComplete()` is CS7036
# just as surely as passing a method the wrong number of arguments. Live on the
# CodeVault run, twice in one file (149,15 and 164,13), because the arity index
# only ever looked at METHODS. `Action` with no generic argument takes none;
# `Action<A,B>` takes two; `Func<A,R>` takes one, since the last type parameter
# is the return.
_TEMPLATE_EVENT_DECL_RE = re.compile(
    r"^[ \t]*(?:\[[^\]\n]*\][ \t]*)*"
    r"(?:public|private|protected|internal)\b[^;(=\n]*?"
    r"\b(?P<kind>Action|Func|UnityAction|UnityEvent)"
    r"(?:<(?P<args>[^>;]*)>)?[ \t]+"
    r"(?P<name>[A-Za-z_]\w*)[ \t]*(?:;|=)")


_JINJA_PLACEHOLDER = "_J_"


# ── Variant-aware template reading ───────────────────────────────────────────
# `_JINJA_TAG_RE` deletes the TAGS and keeps every branch's BODY, so a member
# declared inside `{% if variant == "checkpoint" %}` counted as in-scope for
# every saveload variant. Live on CodeBranchSim: an authored `Update()` called
# `SetCheckpoint()`, which the template really does declare — for one variant
# out of three — and the emitted file did not have it. CS0103, past a gate
# whose whole job is CS0103.
#
# `variant` is not the only thing a template branches on, and reading only that
# one left the same hole open for every other enum. Live on a
# RepoQuestEnterprise build, EnemySpawner.cs: the spawner template declares
# `spawnPoints` inside `{% if s.spawn_shape == "spawn_points" %}`, the schema
# said `point`, and the two authored methods that indexed `spawnPoints` sailed
# through as in-scope. So the schema's own string fields (`s.spawn_shape`,
# `s.pooling`, `s.backend`, …) are decided here too — same rule, wider reach.
_JINJA_BRANCH_RE = re.compile(r"\{%-?\s*(if|elif|else|endif)\b([^%]*?)-?%\}", re.S)
_ATOM_EQ_RE = re.compile(
    r"""^\s*(?:s\.)?(?P<name>[A-Za-z_]\w*)\s*(?P<op>==|!=)\s*['"](?P<lit>[^'"]*)['"]\s*$""")
_ATOM_IN_RE = re.compile(
    r"""^\s*(?:s\.)?(?P<name>[A-Za-z_]\w*)\s+(?P<op>not\s+in|in)\s*"""
    r"""[\(\[](?P<opts>[^)\]]*)[\)\]]\s*$""")
# The OPPOSITE shape from `_ATOM_IN_RE` above: a literal on the left, a bare
# list-typed field on the right — `{% if "boost" in s.abilities %}`, the form
# every ability-gated block in Controller.cs.j2 uses (35+ occurrences: jump,
# dash, shoot, block, grab, pause, ...). None of them matched `_ATOM_IN_RE`
# (that pattern needs a literal `(...)`/`[...]` set on the right, not a bare
# field), so every one of them fell through _eval_atom as undecidable — and
# the "keep the body when undecided" default counted EVERY ability's fields as
# always-declared-by-the-template, regardless of this schema's real abilities
# list. Confirmed live: an authored `_boostTimer`/`_boostCooldownTimer` got
# deleted as a false CS0102 duplicate of a block that, for this schema's
# actual (boost-less) abilities list, never rendered — leaving CS0103s at
# every surviving usage. See render_facts's list[str] handling for the write
# side this reads.
_ATOM_MEMBERSHIP_RE = re.compile(
    r"""^\s*['"](?P<lit>[^'"]*)['"]\s+(?P<op>not\s+in|in)\s+(?:s\.)?(?P<name>[A-Za-z_]\w*)\s*$""")
_LIST_FACT_SEP = "\x1f"
# Split a condition on ONE boolean operator. A condition mixing `and` with `or`
# is left undecided rather than guessed at — precedence is not worth reasoning
# about here, and an undecided condition only widens the scope.
_BOOL_SPLIT_RE = re.compile(r"\s+(and|or)\s+")


def _eval_atom(cond: str, facts: "dict[str, str]") -> "bool | None":
    """True/False for a single `<field> == "literal"` style test, or None when
    the field is not one this pass knows the value of."""
    m = _ATOM_EQ_RE.match(cond or "")
    if m:
        value = facts.get(m.group("name"))
        if value is None:
            return None
        return (value == m.group("lit")) if m.group("op") == "==" else (value != m.group("lit"))
    m = _ATOM_IN_RE.match(cond or "")
    if m:
        value = facts.get(m.group("name"))
        if value is None:
            return None
        options = {v.strip().strip("\"'") for v in m.group("opts").split(",") if v.strip()}
        return (value not in options) if m.group("op").startswith("not") else (value in options)
    m = _ATOM_MEMBERSHIP_RE.match(cond or "")
    if m:
        value = facts.get(m.group("name"))
        if value is None:
            return None
        members = value.split(_LIST_FACT_SEP) if value else []
        is_member = m.group("lit") in members
        return (not is_member) if m.group("op").startswith("not") else is_member
    return None


def _eval_render_condition(cond: str, facts: "dict[str, str]") -> "bool | None":
    """True/False for a condition built purely from fields in `facts`; None when
    any part of it tests something else. None means "keep the body" — this pass
    only ever removes what it can PROVE this schema will not render."""
    parts = _BOOL_SPLIT_RE.split(cond or "")
    ops = {parts[i] for i in range(1, len(parts), 2)}
    if len(ops) > 1:
        return None                        # mixed and/or — do not guess precedence
    values = [_eval_atom(p, facts) for p in parts[0::2]]
    if any(v is None for v in values):
        return None
    return all(values) if ops == {"and"} else any(values)


def _drop_dead_variant_branches(raw: str, variant: str,
                                facts: "tuple[tuple[str, str], ...]" = ()) -> str:
    """Remove the bodies of branches this render will not produce.

    `facts` carries the schema's own string AND list[str] fields (the latter
    joined on `_LIST_FACT_SEP` — see render_facts) as `((name, value), ...)`;
    `variant` is folded in under both `variant` and `s.variant`, the two spellings
    the templates use. Conservative in one direction only: a condition this
    cannot decide keeps its body, so the scope can still be too WIDE but never
    too narrow — a narrow scope would accuse legal code."""
    if "{%" not in raw:
        return raw
    known: "dict[str, str]" = dict(facts)
    if variant:
        known.setdefault("variant", variant)
    if not known:
        return raw
    out: list[str] = []
    pos = 0
    keep: list[bool] = []
    for m in _JINJA_BRANCH_RE.finditer(raw):
        if all(keep):
            out.append(raw[pos:m.start()])
        pos = m.end()
        kind = m.group(1)
        if kind == "if":
            keep.append(_eval_render_condition(m.group(2), known) is not False)
        elif kind == "elif" and keep:
            keep[-1] = _eval_render_condition(m.group(2), known) is not False
        elif kind == "else" and keep:
            keep[-1] = True
        elif kind == "endif" and keep:
            keep.pop()
    if all(keep):
        out.append(raw[pos:])
    return "".join(out)


# Fields whose VALUE is free-text rather than an enum. A template never branches
# on them, and feeding prose to the condition evaluator only risks a spurious
# match, so they are left out of the facts.
_NON_ENUM_FACTS = frozenset({"name", "description"})


def render_facts(schema) -> "tuple[tuple[str, str], ...]":
    """The schema's string AND list[str] fields as a hashable `((name, value), ...)`.

    These are what a template's `{% if s.<field> == "…" %}` / `{% if "x" in
    s.<field> %}` tests, so they are what decides which of its members the
    produced file will really have. Sorted so the tuple is a stable cache key.

    A list[str] field (e.g. ControllerSchema.abilities) is stored joined on
    `_LIST_FACT_SEP` so the tuple stays a flat str->str mapping — see
    `_eval_atom`'s membership branch for the read side. An empty list is still
    recorded (as `""`), deliberately: a field's ABSENCE from facts means
    "unknown, cannot decide" (keeps the branch), but a field present with no
    members means "known — nothing is in it", which correctly decides a bare
    `"x" in s.field` test to False instead of leaving it undecided.
    """
    fields = getattr(type(schema), "model_fields", None) or {}
    out: "dict[str, str]" = {}
    for fname in fields:
        if fname in _NON_ENUM_FACTS:
            continue
        value = getattr(schema, fname, None)
        if isinstance(value, str):
            out[fname] = value
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            out[fname] = _LIST_FACT_SEP.join(value)
    return tuple(sorted(out.items()))


# Some micro folders hold WHOLE-FILE variant alternatives instead of the
# usual always-co-existing sibling classes (SaveData.cs.j2 alongside
# SaveSystem.cs.j2, AIState.cs.j2 alongside AI.cs.j2, ...) — write_files picks
# exactly ONE of these per variant (see e.g. ControllerPipeline.write_files).
# Scanning every file in the folder unconditionally, the way the sibling-class
# case correctly does, falsely puts a pointer-only method like ApplyHighlight
# (PointerController.cs.j2) "in scope" for a multi_mode-variant fragment,
# which never sees that file at all. Confirmed live, PlayerController.cs(565)
# CS0103 that survived `bind_authored_code` entirely because the scan folded
# PointerController.cs.j2's members into scope for every variant. Keyed by the
# directory's own name (== the micro `kind`) since this table only needs to
# name the one directory it's true for.
_VARIANT_ALTERNATIVE_TEMPLATES: dict[str, dict[str, str]] = {
    "controller": {
        "default":    "Controller.cs.j2",
        "pointer":    "PointerController.cs.j2",
        "multi_mode": "ControllerMultiMode.cs.j2",
    },
}


@lru_cache(maxsize=64)
def _template_files(template_dir: str, variant: str = "",
                    facts: "tuple[tuple[str, str], ...]" = ()
                    ) -> "tuple[tuple[str, str, str], ...]":
    """`((filename, jinja-neutralised C#, raw source), ...)` for the micro's folder.

    The raw source is kept because the injection points are Jinja and nothing
    else — `{% for f in s.extra_fields %}` is deleted and `{{ inject.methods(s) }}`
    collapses to a placeholder, so a marker search over the neutralised text
    finds nothing at all.
    """
    root = Path(template_dir)
    if not root.is_dir():
        return ()
    alt = _VARIANT_ALTERNATIVE_TEMPLATES.get(root.name)
    selected = alt.get(variant, alt.get("default")) if alt else None
    alt_filenames = set(alt.values()) if alt else set()
    out: list[tuple[str, str, str]] = []
    for p in sorted(root.glob("*.cs.j2")):
        if p.name in alt_filenames and p.name != selected:
            continue
        raw = p.read_text(encoding="utf-8", errors="replace")
        live = _drop_dead_variant_branches(raw, variant, facts)
        out.append((p.name,
                    _JINJA_EXPR_RE.sub(_JINJA_PLACEHOLDER,
                                       _JINJA_TAG_RE.sub("", live)),
                    raw))
    return tuple(out)


@lru_cache(maxsize=64)
def _template_text(template_dir: str, variant: str = "",
                   facts: "tuple[tuple[str, str], ...]" = ()) -> str:
    """Every C# template in the micro's own folder, with Jinja neutralised.

    `variant` and `facts` drop the branches this render will not produce —
    without them the scope is the union of every variant and every enum value,
    which is how a member that exists in exactly one of them passed as in-scope
    for all of them."""
    return "\n".join(text for _, text, _raw in _template_files(template_dir, variant, facts))


@lru_cache(maxsize=64)
def template_members(template_dir: str, variant: str = "",
                     facts: "tuple[tuple[str, str], ...]" = ()) -> frozenset[str]:
    """Member names the micro's template already declares."""
    names: set[str] = set()
    for line in _template_text(template_dir, variant, facts).splitlines():
        if _TYPE_DECL_RE.search(line):
            continue
        m = _TEMPLATE_MEMBER_RE.match(line)
        if m:
            names.add(m.group(1))
    return frozenset(names)


# Which file's members are actually IN SCOPE for authored code: the one the
# extras are injected into. A micro folder can hold more than one class, and a
# sibling class's fields are not in scope — they are reachable only through an
# instance, which is a member access this module never judges. Hit live on the
# TurboToys dialogue micro: `Node.cs.j2` declares `public string text;`, so a
# folder-wide member set swore `text` was in scope and
# SparkyDialogue.cs(120,16) CS0103 shipped.
#
# Deliberately NOT applied to `template_members`, whose job is the opposite —
# reserving a name so an authored member cannot redeclare it (CS0102). There,
# over-reserving is the documented safe direction.
_INJECTION_MARKERS = ("extra_methods", "extra_fields", "extra_usings",
                      "awake_extra", "update_extra", "fixed_update_extra",
                      "inject.")


def _has_injection_marker(raw: str) -> bool:
    """True if `raw` genuinely RENDERS one of `_INJECTION_MARKERS` — i.e. the
    marker text appears outside a `//`/`///`/`/* */` comment.

    A plain substring search over `raw` self-defeats the moment a file's OWN
    comment explains that it does NOT render extras: DialogTrigger.cs.j2 says
    so in exactly those words ("This class is intentionally NOT an extras
    injection point: extra_fields/extra_methods/extra_usings render only into
    DialogPlayer.cs.j2..."), which made the substring search treat it as an
    injection site anyway. `template_scope_members` then folded DialogTrigger's
    own `_player` field into scope for DialogPlayer's authored code — confirmed
    live, NpcDialogPlayer.cs(150) CS0103, `_player` does not exist on that
    class at all. `_code_mask` already answers "is this position code, not a
    comment or string?" for exactly this reason.
    """
    mask = _code_mask(raw)
    for mark in _INJECTION_MARKERS:
        start = 0
        while True:
            idx = raw.find(mark, start)
            if idx < 0:
                break
            if mask[idx]:
                return True
            start = idx + 1
    return False


@lru_cache(maxsize=64)
def template_scope_members(template_dir: str, variant: str = "",
                           facts: "tuple[tuple[str, str], ...]" = ()) -> frozenset[str]:
    """Member names in scope for code injected into this micro's template.

    Excludes members declared inside a NESTED class/struct — a nested type's
    field is reachable only through an instance of it (`step.ask`, never bare
    `ask`), the same principle `_has_injection_marker` already applies across
    sibling FILES. A flat, brace-depth-blind scan cannot tell a nested type's
    own member from the outer (injectable) class's, so it folded both into
    one flat name set. Confirmed live, GameRules.cs(122) CS0103: `Step` is a
    small nested data class (`public string ask;` among its fields) and the
    authored `update_extra` assigned bare `ask = "...";` — `bind_authored_code`
    judged it against a scope that included every `Step` field as if GameRules
    itself declared them, so the hallucinated reference was never flagged.

    Depth-tracked rather than a real parser: `pending_since` remembers the
    depth a nested type_decl was seen at, covering the Allman-style "class
    Foo" / "{" split across two lines every template here uses (`pending_since`
    persists across any blank/attribute lines between them); `nested_from` is
    the depth the nested type's OWN body starts at once its "{" is seen, so
    its closing brace — which drops depth back BELOW that — is what ends the
    exclusion. Good enough for machine-generated templates that never
    obfuscate braces in strings/comments on these particular lines.
    """
    injecting = [text for _name, text, raw in _template_files(template_dir, variant, facts)
                 if _has_injection_marker(raw)]
    if not injecting:
        # No identifiable injection site — fall back to the folder-wide set
        # rather than claiming an empty scope, which would accuse everything.
        return template_members(template_dir, variant, facts)
    names: set[str] = set()
    for text in injecting:
        depth = 0
        nested_from: "int | None" = None
        pending_since: "int | None" = None
        for line in text.splitlines():
            is_type_decl = bool(_TYPE_DECL_RE.search(line))
            if not is_type_decl and nested_from is None:
                m = _TEMPLATE_MEMBER_RE.match(line)
                if m:
                    names.add(m.group(1))
            if (is_type_decl and depth >= 1
                    and nested_from is None and pending_since is None):
                pending_since = depth        # awaiting this nested type's own "{"
            depth += line.count("{") - line.count("}")
            if pending_since is not None and depth > pending_since:
                nested_from = depth
                pending_since = None
            if nested_from is not None and depth < nested_from:
                nested_from = None
    return frozenset(names)


@lru_cache(maxsize=64)
def template_types(template_dir: str, variant: str = "",
                   facts: "tuple[tuple[str, str], ...]" = ()) -> frozenset[str]:
    """Type names the micro's own template declares.

    `template_members` deliberately skips a `class`/`struct`/`enum` line so a
    base type is not reserved as a member; the names on those lines are still
    real, referable types in the produced file (saveload's `SaveSystemData`,
    which the syntax gate's docstring names as exactly the kind of symbol only
    binding can decide). The unbound-identifier gate needs them or it would
    flag `new SaveSystemData()` as unresolvable.
    """
    return frozenset(_TEMPLATE_TYPE_RE.findall(_template_text(template_dir, variant, facts)))


@lru_cache(maxsize=64)
def template_method_arities(template_dir: str, variant: str = "",
                            facts: "tuple[tuple[str, str], ...]" = ()
                            ) -> "dict[str, tuple[int, int]]":
    """`{method_name: (min_args, max_args)}` for the methods the template declares.

    A parameter with a `=` default is optional, so the accepted count is a RANGE.
    Overloads widen the range rather than being tracked separately — the point is
    to catch a call that matches NO overload (CS7036), and a range that spans two
    real signatures can only ever under-report, never accuse a legal call.
    """
    out: "dict[str, tuple[int, int]]" = {}
    for line in _template_text(template_dir, variant, facts).splitlines():
        if _TYPE_DECL_RE.search(line):
            continue

        # Events first: `public event Action<string> OnDone;` also matches the
        # method regex (`Action<string>` reads as a return type and `OnDone` as
        # a name), so testing methods first would index every event as a
        # zero-argument method and make a legal one-argument invocation look
        # wrong — the exact opposite of the bug being fixed.
        e = _TEMPLATE_EVENT_DECL_RE.match(line)
        if e:
            raw = (e.group("args") or "").strip()
            args = [a for a in _split_top_level_args(raw) if a.strip()] if raw else []
            n = len(args)
            if e.group("kind") == "Func" and n:
                n -= 1          # the last type parameter of Func is the RETURN
            prev = out.get(e.group("name"))
            out[e.group("name")] = ((min(n, prev[0]), max(n, prev[1])) if prev
                                    else (n, n))
            continue

        m = _TEMPLATE_METHOD_DECL_RE.match(line)
        if not m:
            continue
        raw = m.group("params").strip()
        params = [p for p in _split_top_level_args(raw) if p.strip()] if raw else []
        lo = sum(1 for p in params if "=" not in p)
        hi = len(params)
        prev = out.get(m.group("name"))
        out[m.group("name")] = ((min(lo, prev[0]), max(hi, prev[1])) if prev
                                else (lo, hi))
    return out


# `_TEMPLATE_METHOD_DECL_RE` deliberately skips over the return type (a lazy
# non-capturing group) since arity never needed it; this sibling captures it
# instead, for `_repair_field_assignment_type_mismatch` below.
_TEMPLATE_METHOD_RETURN_RE = re.compile(
    r"^[ \t]*(?:\[[^\]\n]*\][ \t]*)*"
    r"(?:public|private|protected|internal)\b(?:\s+(?:static|virtual|override|async|sealed))*"
    r"\s+(?P<ret>[A-Za-z_]\w*)\s+(?P<name>[A-Za-z_]\w*)\s*\(")


@lru_cache(maxsize=64)
def template_method_return_types(template_dir: str, variant: str = "",
                                 facts: "tuple[tuple[str, str], ...]" = ()
                                 ) -> "dict[str, str]":
    """{method_name: return_type} for methods the template declares.

    Sibling to `template_method_arities` (arity) — feeds the CS0029 shape
    `_repair_field_assignment_type_mismatch` checks: a field assigned the
    result of a template-native method whose declared return type doesn't
    match the field's own declared type. `void` is excluded — nothing
    meaningful is ever assigned from it, so its presence could only produce a
    spurious "field = void" flag.
    """
    out: "dict[str, str]" = {}
    for line in _template_text(template_dir, variant, facts).splitlines():
        m = _TEMPLATE_METHOD_RETURN_RE.match(line)
        if m and m.group("ret").lower() != "void":
            out.setdefault(m.group("name"), m.group("ret"))
    return out


def template_void_methods(template_dir: str, variant: str = "",
                          facts: "tuple[tuple[str, str], ...]" = ()
                          ) -> frozenset[str]:
    """Names of methods the template declares with a void return type.

    Sibling to template_method_return_types just above, which deliberately
    EXCLUDES these -- ground truth for the opposite question:
    find_void_method_negated needs to know which calls return nothing, not
    what non-void type the others return.
    """
    out: set[str] = set()
    for line in _template_text(template_dir, variant, facts).splitlines():
        m = _TEMPLATE_METHOD_RETURN_RE.match(line)
        if m and m.group("ret").lower() == "void":
            out.add(m.group("name"))
    return frozenset(out)


@lru_cache(maxsize=64)
def template_usings(template_dir: str) -> frozenset[str]:
    """Namespaces the micro's template already imports.

    `_JINJA_PLACEHOLDER` is excluded: `using {{ u }};` is the extra_usings loop,
    which imports whatever the schema carries — not a namespace of its own.
    """
    return frozenset(ns for ns in _TEMPLATE_USING_RE.findall(_template_text(template_dir))
                     if ns != _JINJA_PLACEHOLDER)


def _method_name(signature: str) -> str:
    m = re.search(r"\b([A-Za-z_]\w*)\s*\(", signature or "")
    return m.group(1) if m else ""


# ── CS0103 / CS7036 — an authored identifier that cannot bind ────────────────
#
# The largest single class of live failure, and the one nothing checked. A
# TurboToys run shipped 49 compile errors across 10 micros; 41 of them were one
# defect repeated — the model authored a fragment referencing state it never
# declared:
#
#   PlayerCamera.cs(60,13)   CS0103  'driftIntensity' does not exist
#   PlayerHealth.cs(51,13)   CS0103  'health' does not exist  (the field is `_current`)
#   PlayerController.cs(250) CS0103  'rb' does not exist
#   SaveSystem.cs(84,59)     CS0103  'FileUtility' does not exist
#   ...Objectives.cs(106,35) CS0103  'IsReachedFinishLine' does not exist
#   UIOverlayShaderController.cs(101,233) CS1061  no 'GetFloatProperty' on this
#   WorldInteractable.cs(87,9)  CS7036  OnTriggerEnter() needs a Collider
#
# Every one is a BINDING error, and the syntax gate is parse-only by design
# ("What this gate does NOT catch", csharp_syntax_gate.py) — all 49 files parse
# perfectly. Nothing downstream looks at names at all.
#
# What makes the check possible now is `template_members`: until it existed the
# per-micro `_RESERVED_FIELD_NAMES` sets were the only notion of what a class
# contains, and they are hand-maintained and incomplete — which is exactly why
# `_drop_unresolvable_types` (unity_micro_code.py) restricted itself to names it
# had removed moments earlier rather than judging identifiers in general. The
# template IS the ground truth for the class, so the judgement is now sound.
#
# Scope is chosen so the ground truth is COMPLETE for what it judges:
#   * a plain identifier (`health`, `rb`, `Sparky`)     -> members + locals
#   * an unqualified or `this.`-qualified call          -> members + local funcs
#   * a type / static head (`FileUtility.Save`)         -> the symbol allowlist
# A member access on something else (`_source.Play()`) is NEVER judged — the
# type of `_source` is not knowable here.
#
# The repair is the one this module already uses for invented Hash natives and
# invented type members: neutralize the STATEMENT, loudly, and never guess a
# replacement. A micro that lost one statement still builds; a micro that keeps
# it takes the whole assembly down.

# Instance members every generated MonoBehaviour inherits. They are real and
# referable but appear nowhere in the template text, so the template-derived
# ground truth cannot see them and would otherwise flag `transform`.
_MONO_INHERITED = frozenset({
    "transform", "gameObject", "name", "tag", "enabled", "isActiveAndEnabled",
    "hideFlags", "useGUILayout", "runInEditMode", "didAwake", "didStart",
    "GetComponent", "GetComponents", "GetComponentInChildren", "TryGetComponent",
    "GetComponentsInChildren", "GetComponentInParent", "GetComponentsInParent",
    "CompareTag", "SendMessage", "SendMessageUpwards", "BroadcastMessage",
    "StartCoroutine", "StopCoroutine", "StopAllCoroutines",
    "Invoke", "InvokeRepeating", "CancelInvoke", "IsInvoking",
    "Instantiate", "Destroy", "DestroyImmediate", "DontDestroyOnLoad",
    "FindObjectOfType", "FindObjectsOfType", "FindAnyObjectByType",
    "FindFirstObjectByType", "FindObjectsByType", "print",
    "Equals", "ReferenceEquals", "GetHashCode", "GetType", "ToString",
})

# Types and static classes a generated MonoBehaviour may name without declaring
# anything. Deliberately generous: a name missing from this set costs one
# commented-out statement and a loud fixer line, so the set errs toward
# admitting a symbol rather than accusing it.
_KNOWN_SYMBOLS = frozenset({
    # C# primitives / BCL
    "bool", "byte", "char", "decimal", "double", "float", "int", "long",
    "object", "sbyte", "short", "string", "uint", "ulong", "ushort", "void",
    "Boolean", "Byte", "Char", "Decimal", "Double", "Int16", "Int32", "Int64",
    "SByte", "Single", "String", "UInt16", "UInt32", "UInt64", "Object",
    "Math", "Convert", "Array", "Enum", "Guid", "DateTime", "DateTimeOffset",
    "TimeSpan", "Environment", "BitConverter", "Buffer", "Activator", "Type",
    "Attribute", "Nullable", "Tuple", "ValueTuple", "KeyValuePair", "Comparer",
    "EqualityComparer", "StringComparison", "StringComparer", "StringSplitOptions",
    "Exception", "ArgumentException", "ArgumentNullException", "IOException",
    "ArgumentOutOfRangeException", "InvalidOperationException", "FormatException",
    "NotImplementedException", "NotSupportedException", "OverflowException",
    "IndexOutOfRangeException", "NullReferenceException", "SystemException",
    "Action", "Func", "Predicate", "Comparison", "EventHandler", "EventArgs",
    "Delegate", "IDisposable", "IComparable", "IEquatable", "IEnumerable",
    "IEnumerator", "ICollection", "IList", "IDictionary", "IReadOnlyList",
    "IReadOnlyCollection", "IReadOnlyDictionary", "List", "Dictionary",
    "HashSet", "Queue", "Stack", "LinkedList", "SortedList", "SortedSet",
    "SortedDictionary", "Enumerable", "StringBuilder", "Encoding", "Regex",
    "Match", "CultureInfo", "NumberStyles", "Path", "File", "Directory",
    "FileInfo", "DirectoryInfo", "StreamReader", "StreamWriter", "MemoryStream",
    "FileMode", "FileAccess", "Task", "Thread", "CancellationToken",
    "Interlocked", "Stopwatch", "Console", "Serializable", "NonSerialized",
    # UnityEngine statics / structs / utility types
    "Application", "Camera", "Cursor", "Debug", "Display", "Event", "Gizmos",
    "Graphics", "GUI", "GUILayout", "GUIUtility", "Handheld", "Input",
    "JsonUtility", "LayerMask", "Mathf", "Physics", "Physics2D", "PlayerPrefs",
    "Profiler", "QualitySettings", "Random", "Resources", "Screen", "Shader",
    "SystemInfo", "Time", "Vector2", "Vector2Int", "Vector3", "Vector3Int",
    "Vector4", "Quaternion", "Matrix4x4", "Color", "Color32", "Gradient",
    "Rect", "RectInt", "RectOffset", "Bounds", "BoundsInt", "Plane", "Ray",
    "Ray2D", "RaycastHit", "RaycastHit2D", "AnimationCurve", "Keyframe",
    "Mesh", "Material", "MaterialPropertyBlock", "Texture", "Texture2D",
    "RenderTexture", "Sprite", "Font", "GUIStyle", "GUIContent", "Debug",
    # UnityEngine components
    "Transform", "RectTransform", "GameObject", "MonoBehaviour", "Component",
    "ScriptableObject", "Behaviour", "Renderer", "MeshRenderer", "MeshFilter",
    "SkinnedMeshRenderer", "SpriteRenderer", "LineRenderer", "TrailRenderer",
    "Rigidbody", "Rigidbody2D", "Collider", "Collider2D", "BoxCollider",
    "SphereCollider", "CapsuleCollider", "MeshCollider", "CharacterController",
    "BoxCollider2D", "CircleCollider2D", "PolygonCollider2D", "EdgeCollider2D",
    "Animator", "Animation", "AnimationClip", "AnimatorStateInfo", "Avatar",
    "AudioSource", "AudioClip", "AudioListener", "AudioMixer", "AudioMixerGroup",
    "Light", "Canvas", "CanvasGroup", "CanvasScaler", "GraphicRaycaster",
    "EventSystem", "ParticleSystem", "NavMeshAgent", "NavMesh", "NavMeshPath",
    "Terrain", "TerrainData", "Joint", "SpringJoint", "HingeJoint",
    "ConfigurableJoint", "CharacterJoint", "WheelCollider", "Skybox",
    "Coroutine", "YieldInstruction", "AsyncOperation", "WaitForSeconds",
    "WaitForSecondsRealtime", "WaitForFixedUpdate", "WaitForEndOfFrame",
    "WaitUntil", "WaitWhile", "Scene", "SceneManager", "LoadSceneMode",
    # enums referenced bare
    "KeyCode", "ForceMode", "ForceMode2D", "Space", "RigidbodyConstraints",
    "RigidbodyInterpolation", "CollisionDetectionMode", "CursorLockMode",
    "TextAnchor", "TextAlignment", "FontStyle", "HorizontalWrapMode",
    "RenderMode", "ScaleMode", "ShadowCastingMode", "LightType", "LightShadows",
    "AudioRolloffMode", "AudioClipLoadType", "WrapMode", "PrimitiveType",
    "SendMessageOptions", "HideFlags", "LogType", "RuntimePlatform",
    "SystemLanguage", "DeviceType", "ScreenOrientation", "FilterMode",
    "TextureWrapMode", "TextureFormat", "RenderTextureFormat", "PlayMode",
    "QueryTriggerInteraction", "AnimatorCullingMode", "AnimatorUpdateMode",
    "AvatarTarget", "AvatarIKGoal", "ParticleSystemStopBehavior",
    # UI / TMP / Input System / misc packages
    "Text", "Image", "RawImage", "Button", "Toggle", "Slider", "Scrollbar",
    "ScrollRect", "Dropdown", "InputField", "Selectable", "Graphic", "Outline",
    "Shadow", "ContentSizeFitter", "LayoutElement", "HorizontalLayoutGroup",
    "VerticalLayoutGroup", "GridLayoutGroup", "AspectRatioFitter", "Mask",
    "TMP_Text", "TMP_InputField", "TMP_Dropdown", "TMP_FontAsset",
    "TextMeshPro", "TextMeshProUGUI", "UIDocument", "VisualElement",
    "InputSystem", "InputAction", "InputActionAsset", "InputActionMap",
    "InputActionReference", "Keyboard", "Mouse", "Gamepad", "Touchscreen",
    "UnityEvent", "UnityAction", "PlayableDirector", "VideoPlayer",
    "SpriteAtlas", "Tilemap", "TileBase", "Volume", "VolumeProfile",
})

_CS_KEYWORDS = frozenset({
    "abstract", "as", "base", "break", "case", "catch", "checked", "class",
    "const", "continue", "default", "delegate", "do", "else", "enum", "event",
    "explicit", "extern", "false", "finally", "fixed", "for", "foreach",
    "goto", "if", "implicit", "in", "interface", "internal", "is", "lock",
    "namespace", "new", "null", "operator", "out", "override", "params",
    "private", "protected", "public", "readonly", "ref", "return", "sealed",
    "sizeof", "stackalloc", "static", "struct", "switch", "this", "throw",
    "true", "try", "typeof", "unchecked", "unsafe", "using", "virtual",
    "volatile", "while",
    # contextual keywords + the compiler-supplied `value` in a setter
    "add", "alias", "and", "ascending", "async", "await", "by", "descending",
    "dynamic", "equals", "from", "get", "global", "group", "init", "into",
    "join", "let", "nameof", "not", "on", "or", "orderby", "partial", "record",
    "remove", "select", "set", "value", "var", "when", "where", "with", "yield",
})

# An identifier occurrence that is a candidate for binding. The lookbehind
# excludes a member access (`Time.deltaTime` -> `deltaTime` is not judged) and
# the tail of a numeric literal (`0.5f` -> the `f` is not an identifier).
_BINDABLE_IDENT_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)")
# `X y` — the second identifier is a DECLARATION (local, parameter, foreach
# variable, catch variable, `is Foo f` pattern) as long as the first token is a
# type and not a keyword. Deliberately generous: over-collecting a declaration
# only ever costs a missed error, while under-collecting accuses a real local.
_DECL_PAIR_RE = re.compile(
    r"(?<![\w.])(?P<type>[A-Za-z_][\w.]*)"
    r"(?:\s*<[^<>;{}()]*>)?(?:\s*\[\s*\])*\s*\??"
    r"\s+(?P<name>[A-Za-z_]\w*)\b")
_VAR_DECL_RE     = re.compile(r"(?<![\w.])(?:var|dynamic)\s+([A-Za-z_]\w*)")
_OUT_REF_DECL_RE = re.compile(
    r"(?<![\w.])(?:out|ref)\s+(?:var\s+|[A-Za-z_][\w.<>,\[\]?]*\s+)?([A-Za-z_]\w*)")
_TUPLE_DECL_RE   = re.compile(r"(?<![\w.])var\s*\(([^)]*)\)")
_LAMBDA_ONE_RE   = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*=>")
_LAMBDA_MANY_RE  = re.compile(r"\(([^()]*)\)\s*=>")
# `this.Member` — unlike any other member access, the ground truth IS complete.
# Hit live: UIOverlayShaderController.cs authored `this.GetFloatProperty(...)`.
_THIS_MEMBER_RE  = re.compile(r"(?<![\w.])this\s*\.\s*([A-Za-z_]\w*)")


def _fragment_declares(text: str) -> set[str]:
    """Every name the fragment itself brings into scope.

    CODE positions only. English prose in a `//` comment parses as a perfectly
    good `<type> <name>` pair, and the phantom declarations that produces are
    silent — they do not break anything, they quietly TEACH this pass that an
    undeclared name is fine. Hit live on the TurboToys controller:

        // Consume boost to increase speed
        rb.AddForce(...);

    `speed` + newline + `rb` read as a declaration of `rb`, so the five real
    CS0103s on `rb` went unflagged. Same defect class as the brace counter and
    the out-param rewriter above, in the direction that is hardest to notice.
    """
    names: set[str] = set()
    if not text:
        return names
    mask = _code_mask(text)

    def add_all(pattern: "re.Pattern[str]", group: int = 1) -> None:
        for m in pattern.finditer(text):
            if mask[m.start()]:
                names.add(m.group(group))

    add_all(_VAR_DECL_RE)
    add_all(_OUT_REF_DECL_RE)
    add_all(_LAMBDA_ONE_RE)
    for pattern in (_TUPLE_DECL_RE, _LAMBDA_MANY_RE):
        for m in pattern.finditer(text):
            if not mask[m.start()]:
                continue
            for part in _split_top_level_args(m.group(1)):
                tokens = [t for t in re.split(r"\s+", part.strip()) if t]
                if tokens:
                    names.add(tokens[-1])
    for m in _DECL_PAIR_RE.finditer(text):
        if not mask[m.start("type")] or not mask[m.start("name")]:
            continue
        if m.group("type") in _CS_KEYWORDS:
            continue                       # `return foo` / `new Foo` declare nothing
        names.add(m.group("name"))
    return names


def _signature_param_names(signature: str) -> set[str]:
    """Parameter names a method signature brings into scope for its own body."""
    m = re.search(r"\((.*)\)", signature or "", re.DOTALL)
    if not m:
        return set()
    out: set[str] = set()
    for raw in _split_top_level_args(m.group(1)):
        part = raw.split("=", 1)[0].strip()
        tokens = [t for t in re.split(r"\s+", part) if t]
        if len(tokens) >= 2:               # "<type> <name>"
            out.add(tokens[-1])
    return out


# ── Argument TYPES (CS1503) ──────────────────────────────────────────────────
# The arity check answers "how many", never "of what", so every one of these
# passed it and failed at Unity:
#     Play("success", 1f)                  Play(string, Vector3?)
#     ApplyHighlight(node.name)            ApplyHighlight(GameObject)
#     OnObjectiveProgress(node, 1f, "x")   Action<string, int, int>
# Judged only where the answer is certain: a literal, a `.name`, or an
# identifier whose type the fragment itself declares. Anything else is unknown
# and unjudged — a checker that guesses would neutralise working code.
_PRIMITIVE_TYPES = frozenset({"string", "int", "float", "double", "bool", "char",
                              "long", "short", "byte", "decimal", "uint"})
# What a literal of type K may be passed as, beyond its own type.
_WIDENS_TO: "dict[str, frozenset[str]]" = {
    "int":    frozenset({"int", "long", "float", "double", "decimal", "short"}),
    "float":  frozenset({"float", "double"}),
    "double": frozenset({"double"}),
    "char":   frozenset({"char", "int", "long", "float", "double", "string"}),
    "string": frozenset({"string"}),
    "bool":   frozenset({"bool"}),
}
_STRING_LIT_RE = re.compile(r'^@?\$?"(?:[^"\\]|\\.)*"$')
_CHAR_LIT_RE   = re.compile(r"^'(?:[^'\\]|\\.)+'$")
_INT_LIT_RE    = re.compile(r"^[+-]?\d+[uUlL]{0,2}$")
_FLOAT_LIT_RE  = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)[fF]$")
_DOUBLE_LIT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+)[dD]?$")
_DOT_NAME_RE   = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\.name$")
# `<chain>.text` is a string on every mainstream Unity UI text component
# (Text.text, TMP_Text.text, InputField.text, TMP_InputField.text) — the same
# "trust the property name" reasoning _DOT_NAME_RE applies to `.name`. Unlike
# `.name`, the chain leading to `.text` commonly runs through a generic method
# call (`GetComponentInChildren<Text>().text`), which _DOT_NAME_RE's pure
# dotted-identifier pattern cannot match at all — so only the trailing member
# access is checked, not the whole expression's shape. Hit live,
# NpcDialogPlayer.cs(152,160): `_tinkerGhostRef.gameObject.
# GetComponentInChildren<Text>().text` (a string) was passed where
# PlayOneLine(NpcDialogLineData) expects the line object itself (CS1503).
_DOT_TEXT_RE   = re.compile(r"\.text$")
# Mathf statics with exactly one, always-float overload. Min/Max/Abs/Clamp are
# excluded on purpose: those are also overloaded on int in the real API, so
# their result type depends on the arguments and this pass cannot see that.
_MATHF_NUMERIC_RE = re.compile(
    r"^Mathf\.(?:Lerp(?:Angle|Unclamped)?|Clamp01|MoveTowards(?:Angle)?|"
    r"PingPong|Repeat|SmoothStep|InverseLerp|DeltaAngle|Sign|"
    r"Pow|Sqrt|Exp|Log(?:10)?|Sin|Cos|Tan|Asin|Acos|Atan2?|Round|Floor|Ceil)"
    r"\s*\(")
# `GameObject go`, `int count` — a declaration the fragment makes itself, in a
# signature or a local. The type it states is ground truth for that name.
_DECL_TYPE_RE = re.compile(
    r"\b([A-Za-z_]\w*(?:<[^<>()]*>)?\??)\s+([A-Za-z_]\w*)\s*(?=[,)=;])")


def _normalise_param_type(raw: str) -> str:
    """`ref Vector3?` -> `vector3?`; `System.String` -> `string`."""
    t = (raw or "").strip()
    for mod in ("ref ", "out ", "in ", "params ", "this "):
        if t.startswith(mod):
            t = t[len(mod):].strip()
    t = t.rsplit(".", 1)[-1]
    alias = {"String": "string", "Int32": "int", "Single": "float",
             "Double": "double", "Boolean": "bool", "Char": "char",
             "Int64": "long"}
    return alias.get(t, t).lower()


def _expression_type(expr: str, locals_: "dict[str, str]") -> str:
    """The C# type of an argument expression, or "" when it is not certain."""
    e = (expr or "").strip()
    if not e:
        return ""
    if e == "null":
        return "null"
    if e in ("true", "false"):
        return "bool"
    if _STRING_LIT_RE.match(e):
        return "string"
    if _CHAR_LIT_RE.match(e):
        return "char"
    if _FLOAT_LIT_RE.match(e):
        return "float"
    if _INT_LIT_RE.match(e):
        return "int"
    if _DOUBLE_LIT_RE.match(e):
        return "double"
    # UnityEngine.Object.name is a string on every Unity type, and passing it
    # where the object itself belongs is the single commonest of these.
    if _DOT_NAME_RE.match(e):
        return "string"
    if _DOT_TEXT_RE.search(e):
        return "string"
    if _MATHF_NUMERIC_RE.match(e):
        return "float"
    return locals_.get(e, "")


_VAR_INIT_RE = re.compile(r"\bvar\s+([A-Za-z_]\w*)\s*=\s*([^;]+);")


def _declared_types(text: str) -> "dict[str, str]":
    """`{identifier: type}` for names the fragment declares, dropping any name
    declared twice with different types — an ambiguous name is not evidence."""
    seen: "dict[str, str]" = {}
    clash: set[str] = set()
    for m in _DECL_TYPE_RE.finditer(text or ""):
        t, name = _normalise_param_type(m.group(1)), m.group(2)
        if t in _CS_KEYWORDS and t not in _PRIMITIVE_TYPES:
            continue
        if name in seen and seen[name] != t:
            clash.add(name)
        seen[name] = t
    # `var x = <expr>;` names no type directly, so the scan above skips it
    # ("var" is a keyword, not a type) — anything `_expression_type` can
    # otherwise prove about the INITIALIZER is recovered here instead, using
    # whatever locals are already known. Only the same small set of certain
    # shapes counts (literals, `.name`, `.text`, a known Mathf static);
    # anything else stays unresolved rather than guessed at. Hit live,
    # NpcDialogPlayer.cs(152,160): `var line = _tinkerGhostRef.gameObject.
    # GetComponentInChildren<Text>().text;` then `PlayOneLine(line)` — CS1503
    # (a string passed where NpcDialogLineData is expected).
    for m in _VAR_INIT_RE.finditer(text or ""):
        name, init = m.group(1), m.group(2).strip()
        if name in seen:
            continue
        t = _expression_type(init, seen)
        if t and t != "null":
            seen[name] = t
    for name in clash:
        seen.pop(name, None)
    return seen


def _arg_conflicts(arg_type: str, param_type: str) -> bool:
    """True only when this argument provably cannot convert to this parameter.

    Every uncertain case answers False. The gate neutralises what it flags, so
    a false positive deletes working code — strictly worse than the CS1503 it
    would have prevented.
    """
    a, p = arg_type, _normalise_param_type(param_type)
    if not a or not p or p in ("object", "var", "dynamic") or "<" in p:
        return False
    nullable = p.endswith("?")
    bare = p.rstrip("?")
    if a == "null":
        # A value type that is not nullable cannot take null; everything else can.
        return bare in _PRIMITIVE_TYPES and not nullable and bare != "string"
    if a == bare:
        return False
    a_prim, p_prim = a in _PRIMITIVE_TYPES, bare in _PRIMITIVE_TYPES
    if a_prim and p_prim:
        return bare not in _WIDENS_TO.get(a, frozenset({a}))
    # One side a primitive and the other a class/struct: no implicit conversion
    # exists in either direction, whatever the class is.
    if a_prim != p_prim:
        return True
    # Two class types — inheritance could make this legal, and this pass cannot
    # see a base list. Never judged.
    return False


@lru_cache(maxsize=64)
def template_method_params(template_dir: str, variant: str = "",
                           facts: "tuple[tuple[str, str], ...]" = ()
                           ) -> "dict[str, tuple[str, ...]]":
    """`{method_name: (param_type, ...)}` for the methods and events the
    template declares, with any OVERLOADED name dropped: two signatures mean a
    call could match either, and this pass cannot tell which was meant."""
    out: "dict[str, tuple[str, ...]]" = {}
    dropped: set[str] = set()

    def record(name: str, types: "list[str]") -> None:
        sig = tuple(_normalise_param_type(t) for t in types)
        if name in out and out[name] != sig:
            dropped.add(name)
        out[name] = sig

    for line in _template_text(template_dir, variant, facts).splitlines():
        if _TYPE_DECL_RE.search(line):
            continue
        e = _TEMPLATE_EVENT_DECL_RE.match(line)
        if e:
            raw = (e.group("args") or "").strip()
            args = [a for a in _split_top_level_args(raw) if a.strip()] if raw else []
            if e.group("kind") == "Func" and args:
                args = args[:-1]            # the last type parameter is the RETURN
            record(e.group("name"), args)
            continue
        m = _TEMPLATE_METHOD_DECL_RE.match(line)
        if m:
            raw = (m.group("params") or "").strip()
            types: list[str] = []
            for part in (_split_top_level_args(raw) if raw else []):
                decl = part.split("=", 1)[0].strip()
                tokens = [t for t in re.split(r"\s+", decl) if t]
                types.append(" ".join(tokens[:-1]) if len(tokens) >= 2 else "")
            record(m.group("name"), types)
    for name in dropped:
        out.pop(name, None)
    return out


# `[SerializeField] private Color ambientGround = ...;` / `private float
# _lastHit = -999f;` — a FIELD declaration, captured with its type. Same
# anchor as `_TEMPLATE_MEMBER_RE` (a member always carries an access
# modifier here), narrowed to the shape with no `(` before the terminator —
# that is exactly what tells a field apart from `_TEMPLATE_METHOD_DECL_RE`.
_TEMPLATE_FIELD_DECL_RE = re.compile(
    r"^[ \t]*(?:\[[^\]\n]*\][ \t]*)*"
    r"(?:public|private|protected|internal)\b"
    r"(?:[ \t]+(?:static|readonly|const|new))*"
    # Generic-argument suffix (List<GameObject>, Dictionary<string, int>) —
    # greedy `.*` inside the bracket group backtracks to the LAST `>` that
    # still leaves a valid name+terminator after it, which for one field
    # declaration on one line is the true outermost close even when the
    # generic itself nests (Dictionary<string, List<int>>). Confirmed live
    # (2026-08-28): the type-capture group had no `<`/`>` at all before this,
    # so it silently failed to match ANY generic-typed field declaration —
    # `private readonly List<GameObject> _alive = ...;` never matched,
    # meaning template_field_types() never saw a collection field exists,
    # and the misuse checks that key off it (find_field_type_misuse,
    # _repair_collection_compared_as_scalar) had no ground truth for one.
    r"[ \t]+(?P<type>[A-Za-z_][\w.]*(?:<.*>)?\??(?:\[\])?)"
    r"[ \t]+(?P<name>[A-Za-z_]\w*)[ \t]*(?:=[^=]|;)")


def _field_type_norm(raw: str) -> str:
    """`UnityEngine.AudioSource` -> `AudioSource`; drops a nullable `?`."""
    t = (raw or "").strip().rstrip("?")
    return t.rsplit(".", 1)[-1]


@lru_cache(maxsize=64)
def template_field_types(template_dir: str, variant: str = "",
                         facts: "tuple[tuple[str, str], ...]" = ()
                         ) -> "dict[str, str]":
    """`{field_name: declared_type}` for fields the micro's templates declare.

    Ground truth for `find_field_type_misuse` below, the same way
    `template_method_arities` is ground truth for a call's argument count. A
    name declared twice with two different types (two sibling classes in the
    same micro folder reusing a field name) is dropped — ambiguous, so never
    grounds to accuse either type.
    """
    seen: "dict[str, str]" = {}
    clash: set[str] = set()
    for line in _template_text(template_dir, variant, facts).splitlines():
        if _TYPE_DECL_RE.search(line):
            continue
        m = _TEMPLATE_FIELD_DECL_RE.match(line)
        if not m:
            continue
        t = _field_type_norm(m.group("type"))
        name = m.group("name")
        if name in seen and seen[name] != t:
            clash.add(name)
        else:
            seen[name] = t
    for name in clash:
        seen.pop(name, None)
    return seen


# CS0200 — authored code assigns directly to a template-declared GET-ONLY,
# expression-bodied property (`GearDown = true;`) instead of its backing
# field. The property's own expression body names that field with complete
# ground truth. Confirmed live, PlayerController.cs(554,555):
# ControllerMultiMode.cs.j2 declares
#     public bool  GearDown => _gearDown;
# and the model, needing to SET gear-down state from an authored method,
# assigned the read-only property itself. `find_unbound_identifiers` does not
# catch this — `GearDown` genuinely IS a known, bound name; the defect is
# that it isn't an assignable one, a check that pass never performs.
_READONLY_PROPERTY_RE = re.compile(
    r"^\s*(?:public|private|protected|internal)\s+[A-Za-z_][\w<>,\[\].?]*\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*=>\s*(?P<backing>[A-Za-z_]\w*)\s*;\s*$",
    re.MULTILINE)
# Bare (non-dotted — same-instance, implicit `this.`) simple assignment to a
# name. Deliberately excludes `==`/`+=`/etc. and a dotted receiver: rewriting
# `other.GearDown = x` would presume `_gearDown` is accessible off `other`,
# which a private backing field never is.
_ASSIGN_TO_NAME_RE = re.compile(
    r"(?<!\.)\b(?P<name>[A-Za-z_]\w*)\b(?P<sp1>[ \t]*)=(?!=)(?P<sp2>[ \t]*)"
    r"(?P<rhs>[^;=\s][^;]*;)")


@lru_cache(maxsize=64)
def template_readonly_property_backing(template_dir: str, variant: str = "",
                                       facts: "tuple[tuple[str, str], ...]" = ()
                                       ) -> "dict[str, str]":
    """`{PropertyName: backing_field_name}` for every get-only, expression-
    bodied property (`public T Name => _field;`) the template declares."""
    out: dict[str, str] = {}
    for line in _template_text(template_dir, variant, facts).splitlines():
        m = _READONLY_PROPERTY_RE.match(line)
        if m:
            out[m.group("name")] = m.group("backing")
    return out


def _repair_readonly_property_assign(text: str, *, backing: "dict[str, str]") -> str:
    """Rewrite `<Prop> = v;` -> `<backing> = v;` for a known get-only property."""
    if not text or not backing:
        return text
    mask = _code_mask(text)
    changed = False

    def repl(m: "re.Match") -> str:
        nonlocal changed
        if not mask[m.start()]:
            return m.group(0)
        field = backing.get(m.group("name"))
        if field is None:
            return m.group(0)
        changed = True
        return f"{field}{m.group('sp1')}={m.group('sp2')}{m.group('rhs')}"

    new = _ASSIGN_TO_NAME_RE.sub(repl, text)
    if changed:
        console.print("  [magenta]FIX[/]  rewrote assignment to a get-only "
                      "template property as its backing field (CS0200)")
    return new


# The rewrite above only fires for a TRIVIAL passthrough body (`Name =>
# _field;`) — it has nowhere to route an assignment when the body is
# anything else, and silently left the assignment untouched rather than
# ever saying so. Confirmed live, DeadReckoningProtocol GameRules.cs(266):
# `CurrentStep += 1;` against Rules.cs.j2's
#     public Step CurrentStep => Finished || steps.Count == 0 ? null : steps[_step];
# — a ternary returning a `Step` object, not a passthrough of any single
# field, so there is no backing name to rewrite INTO; the assignment is
# simply illegal (CS0200) and must be neutralized like any other
# unrepairable misuse. `_ASSIGN_TO_NAME_RE` also only ever matched a bare
# `=`, so `+=` here was invisible to the rewrite pass in the first place —
# not simply un-rewritable, but never even detected.
_READONLY_PROPERTY_ANY_BODY_RE = re.compile(
    r"^\s*(?:public|private|protected|internal)\s+[A-Za-z_][\w<>,\[\].?]*\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*=>\s*[^;]+;\s*$",
    re.MULTILINE)
_READONLY_ASSIGN_RE = re.compile(
    r"(?<!\.)\b(?P<name>[A-Za-z_]\w*)\b[ \t]*"
    r"(?:\+=|-=|\*=|/=|(?<![=!<>])=(?!=))[ \t]*[^;=\s][^;]*;")


@lru_cache(maxsize=64)
def template_readonly_property_names(template_dir: str, variant: str = "",
                                     facts: "tuple[tuple[str, str], ...]" = ()
                                     ) -> "frozenset[str]":
    """Every get-only, expression-bodied property NAME the template declares,
    regardless of body complexity — a strict superset of
    `template_readonly_property_backing`'s keys (that one additionally
    requires a trivial single-identifier body to know what to rewrite an
    assignment INTO). Ground truth for `find_readonly_property_misuse`."""
    return frozenset(
        m.group("name")
        for line in _template_text(template_dir, variant, facts).splitlines()
        for m in [_READONLY_PROPERTY_ANY_BODY_RE.match(line)] if m)


def find_readonly_property_misuse(text: str, *, readonly_names: "frozenset[str]",
                                  signature: str = "") -> "dict[int, set[str]]":
    """`{line_index: {description}}` for an assignment (`=`/`+=`/`-=`/...)
    into a name the template declares as a get-only, expression-bodied
    property (CS0200) — the complement to `_repair_readonly_property_assign`:
    that one rewrites the simple, value-preserving case (a trivial
    passthrough body); this one catches everything else — a complex body
    (ternary, indexer, method call, ...) has no single backing field to
    rewrite into, so the assignment is refused outright, the same as any
    other unrepairable misuse this module neutralizes."""
    if not text or not readonly_names:
        return {}
    mask = _code_mask(text)
    shadowed = _fragment_declares(text) | _signature_param_names(signature)
    bad: "dict[int, set[str]]" = {}
    for m in _READONLY_ASSIGN_RE.finditer(text):
        if not mask[m.start()]:
            continue
        name = m.group("name")
        if name not in readonly_names or name in shadowed:
            continue
        bad.setdefault(text.count("\n", 0, m.start()), set()).add(
            f"{name} -- {name} is a read-only property, it has no setter")
    return bad


def lint_readonly_property_misuse(text: str, *, readonly_names: "frozenset[str]",
                                  signature: str = "") -> "tuple[str, set[str]]":
    """Neutralize an assignment into a get-only template property whose body
    is too complex for `_repair_readonly_property_assign` to rewrite. Call
    this AFTER that rewrite, on its output: a property it already rewrote no
    longer has an assignment to its OWN name left in the text (the statement
    now assigns the backing field instead), so the two passes never conflict
    — this one only ever sees what the first pass could not fix."""
    bad = find_readonly_property_misuse(text, readonly_names=readonly_names,
                                        signature=signature)
    if not bad:
        return text, set()
    return _neutralize_flagged_lines(text, bad, reason="assignment(s) to a read-only property")


def find_unbound_identifiers(text: str, *, known: "set[str] | frozenset[str]",
                             arities: "dict[str, tuple[int, int]] | None" = None,
                             params: "dict[str, tuple[str, ...]] | None" = None,
                             signature: str = "",
                             field_types: "dict[str, str] | None" = None
                             ) -> "dict[int, set[str]]":
    """`{line_index: {name, ...}}` for identifiers in `text` that cannot bind.

    `known` is the complete set of names in scope — template members and types,
    authored fields and methods, inherited MonoBehaviour members, the symbol
    allowlist, and whatever the fragment declares locally (added here).
    `arities` additionally checks the argument COUNT of a call against a
    template-declared method (CS7036).
    `field_types` seeds the argument-TYPE check (see `params` below) with the
    class's OWN field types — a field is used, never declared, inside the one
    fragment being scanned, so `_declared_types` alone never sees it. Hit
    live, BossBattleTinkerGhostObjectives.cs(131): `ReportProgress(_bossHealth,
    _maxBossHealth)` passed two `[SerializeField] private float` fields where
    `ReportProgress(string, int)` needs a string and an int (CS1503 x2) — both
    fields are declared elsewhere in the class (`extra_fields`), invisible to
    a check that only reads the fragment's own text.
    """
    if not text:
        return {}
    mask = _code_mask(text)
    scope = set(known) | _fragment_declares(text) | _CS_KEYWORDS
    bad: "dict[int, set[str]]" = {}

    def flag(pos: int, name: str) -> None:
        bad.setdefault(text.count("\n", 0, pos), set()).add(name)

    for m in _BINDABLE_IDENT_RE.finditer(text):
        start, name = m.start(1), m.group(1)
        if not mask[start] or name in scope:
            continue
        after = text[m.end(1):]
        # A named argument (`Foo(speed: 3)`), a label, or a `case X:` arm names
        # something this pass cannot see. Skipping costs a missed error only.
        if re.match(r"\s*:(?!:)", after):
            continue
        call = re.match(r"\s*(?:<[^<>;{}()]*>)?\s*\(", after)
        if call:
            flag(start, f"{name}()")
        else:
            flag(start, name)

    # `this.Member` — the only member access with complete ground truth.
    for m in _THIS_MEMBER_RE.finditer(text):
        if mask[m.start()] and m.group(1) not in scope:
            flag(m.start(1), f"this.{m.group(1)}")

    if arities or params:
        # The enclosing signature declares the types of the names its body
        # uses — without it `OnObjectiveProgress(node, ...)` has no idea what
        # `node` is, and the commonest wrong argument is a parameter passed on.
        # `field_types` is seeded FIRST so a local/parameter declared with the
        # same name in this fragment (shadowing) wins, same precedence a real
        # compiler gives the closer scope.
        locals_ = dict(field_types or {})
        if params:
            locals_.update(_declared_types(signature + "\n" + text))
        for m in _BINDABLE_IDENT_RE.finditer(text):
            name = m.group(1)
            if not mask[m.start(1)]:
                continue
            if name not in (arities or {}) and name not in (params or {}):
                continue
            paren = re.match(r"\s*\(", text[m.end(1):])
            if not paren:
                continue
            open_at = m.end(1) + paren.end() - 1
            close = _find_matching_brace_generic(text, open_at, "(", ")", mask)
            if close < 0:
                continue
            inner = text[open_at + 1:close].strip()
            args = [p for p in _split_top_level_args(inner) if p.strip()] if inner else []
            count = len(args)
            if arities and name in arities:
                lo, hi = arities[name]
                if not (lo <= count <= hi):
                    flag(m.start(1), f"{name}({count} args, needs {lo}..{hi})")
                    continue
            # Types only once the count fits: reporting both for one call would
            # name the same defect twice and say neither clearly.
            wanted = (params or {}).get(name)
            if not wanted or count > len(wanted):
                continue
            for i, arg in enumerate(args):
                got = _expression_type(arg, locals_)
                if _arg_conflicts(got, wanted[i]):
                    flag(m.start(1),
                         f"{name}(arg {i + 1} is {got}, needs {wanted[i].strip()})")
                    break
    return bad


def _find_matching_brace_generic(text: str, open_idx: int, opener: str,
                                 closer: str, mask: bytearray) -> int:
    """Index of the `closer` matching `text[open_idx]`, or -1. CODE only."""
    depth = 0
    for i in range(open_idx, len(text)):
        if not mask[i]:
            continue
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return i
    return -1


# A single-line `if (cond) stmt;` is a complete statement, so commenting it out
# looks safe — and is not, when an `else` follows. The `else` is left with no
# `if` to attach to (CS1525/CS8641), and a syntax error is a worse trade than
# the CS0103 being repaired. Hit on the RepoQuestEnterprise spawner the moment
# `spawnPoints` became correctly unbindable:
#
#     if (spawnPoints != null && spawnPoints.Count > 0) _spawnPointIndex = 0;
#     else if (_spawnPointIndex >= spawnPoints.Length - 1) _spawnPointIndex = 0;
#     else _spawnPointIndex++;                         <- both heads commented
#
# So an `if`/`else if` head is DISABLED rather than deleted: the chain keeps its
# shape, the guarded statement stops running, and the author's surviving `else`
# still has its branch. Only these two keywords — a `for`/`foreach`/`while` line
# leaves nothing dangling behind it and is commented out as before.
#
# And only when an `else` really follows. A comment is the better outcome
# everywhere else, because `if (false) { }` is a LIVE line to every pass that
# reads one — `_body_is_live` would keep a method whose every statement had just
# been neutralized, instead of dropping it.
_IF_HEAD_RE = re.compile(
    r'^(?P<indent>[ \t]*)(?P<lead>\}[ \t]*)?(?P<kw>else[ \t]+if|if)[ \t]*\(')
_ELSE_HEAD_RE = re.compile(r'^[ \t]*(?:\}[ \t]*)?else\b')


def _stub_if_head(line: str, names: "list[str] | set[str]", *,
                  followed_by_else: bool,
                  reason: str = "unbindable reference(s)") -> "str | None":
    """`if (cond) stmt;` → `if (false) { }`, keeping the if/else chain intact."""
    if not followed_by_else:
        return None
    m = _IF_HEAD_RE.match(line)
    if not m:
        return None
    kw = re.sub(r"[ \t]+", " ", m.group("kw"))
    return (f"{m.group('indent')}{m.group('lead') or ''}{kw} (false) {{ }} "
            f"// AUDIT: removed {reason} "
            f"{', '.join(sorted(names))} -- {line.strip()}")


# A braceless header governs the ONE statement after it. `_disable_bad_header`
# turns the condition into `false` and leaves that statement in place as dead
# code, which is right — until the statement is itself neutralized, and the
# header silently adopts the next live one instead:
#
#     if (false) /* AUDIT */
#         // AUDIT: return spawnPoints[_spawnPointIndex].position;
#     return transform.position + ...;      <- now the if's body; CS0161
#
# The method's only unconditional return became conditional on `false`. Giving
# the header an empty block closes it off — but only when its body really was
# removed, since a surviving body must stay inside it.
def _governed_body_line(lines: "list[str]", i: int) -> int:
    """Index of the statement a braceless header at `i` governs, or -1."""
    for j in range(i + 1, len(lines)):
        if lines[j].strip():
            return j
    return -1


def _neutralize_flagged_lines(text: str, bad: "dict[int, set[str]]", *,
                              reason: str = "unbindable reference(s)"
                              ) -> "tuple[str, set[str]]":
    """Apply the shared three-way repair to every line `bad` names: comment
    out a complete self-terminating statement, swap an `if`/`while`
    condition for `false` when the statement spans lines, or leave the line
    alone entirely when neither is safe — because a repair that trades one
    compile error for a parse error has not repaired anything.

    Shared by every `lint_*` pass in this module that flags whole LINES
    rather than proposing its own rewrite: unbound identifiers (CS0103/
    CS1061/CS7036) and template-field type misuse (CS1061/CS1503) both
    reduce to this same problem once they've located the bad line. `reason`
    only changes the wording of the AUDIT comment left behind.
    """
    lines = text.splitlines()
    out: list[str] = []
    flagged: set[str] = set()

    def _next_is_else(i: int) -> bool:
        j = _governed_body_line(lines, i)
        return j != -1 and bool(_ELSE_HEAD_RE.match(lines[j]))

    def _becomes_comment(j: int) -> bool:
        """True when line `j` will be replaced by a comment — the only outcome
        that leaves a braceless header above it with nothing to govern."""
        if j < 0 or j >= len(lines):
            return True                    # ran off the end: nothing to govern
        if lines[j].lstrip().startswith("//"):
            return True
        return (bool(bad.get(j)) and _safe_to_comment_out(lines[j])
                and _stub_if_head(lines[j], bad[j],
                                  followed_by_else=_next_is_else(j),
                                  reason=reason) is None)

    for i, line in enumerate(lines):
        names = bad.get(i)
        if not names or line.lstrip().startswith("//"):
            out.append(line)
            continue
        if not _safe_to_comment_out(line):
            disabled = _disable_bad_header(line, names)
            if disabled is None:
                out.append(line)           # neither repair is safe — report only
                continue
            flagged.update(names)
            # `_disable_bad_header` re-emits the header's own `{` when it had
            # one; only a BRACELESS header can adopt the statement below it.
            head = _DISABLEABLE_HEADER_RE.match(line)
            if head is not None and not head.group("brace") \
                    and _becomes_comment(_governed_body_line(lines, i)):
                disabled += " { }"
            out.append(disabled)
            continue
        stub = _stub_if_head(line, names, followed_by_else=_next_is_else(i),
                             reason=reason)
        if stub is not None:
            flagged.update(names)
            out.append(stub)
            continue
        flagged.update(names)
        indent = line[: len(line) - len(line.lstrip())]
        out.append(f"{indent}// AUDIT: removed {reason} "
                   f"{', '.join(sorted(names))} -- {line.strip()}")
    return "\n".join(out), flagged


# CS0103 — the author declared `_grid` and then wrote `grid`.
#
# Like lint_undeclared_catch_exception_var above, this is a fully MECHANICAL
# fix rather than a neutralize-the-line fallback, and it is safe for the same
# reason: the name being rewritten provably does not bind (it is in `unbound`),
# so no code that compiles today can change meaning.
#
# It matters more than the compile error it clears. The alternative repairs are
# both wrong on CONTENT: neutralizing the statement deletes behaviour the author
# meant to write, and DECLARING a second `grid` field leaves two references to
# the same collaborator where only one is ever wired in the scene — the script
# compiles and then silently does nothing at runtime. Rebinding to the field
# the author already declared is the only repair that keeps the intent.
#
# Hit live on Gridfall's InputHandler: `[SerializeField] private GridLogic
# _grid` declared, `if (_grid == null) return;` guarding, then
# `grid.SetScore(grid.GetScore() + 1);` — three CS0103s in one file.
_ALIAS_SKIP_RE = re.compile(
    r'"(?:\\.|[^"\\])*"'        # string literal
    r"|'(?:\\.|[^'\\])*'"       # char literal
    r"|//[^\n]*"                  # line comment
    r"|/\*.*?\*/",                # block comment
    re.DOTALL,
)


def lint_underscore_alias_references(text: str, *, unbound: "set[str]",
                                     declared: "set[str] | frozenset[str]"
                                     ) -> "tuple[str, set[str]]":
    """Rewrite an unbound `name` to `_name` when `_name` IS a declared member.

    Returns `(text, rebound_names)`. Only names present in `unbound` are ever
    touched, so a genuine local variable called `grid` is left alone. String
    and comment regions are skipped so prose is never rewritten.
    """
    targets = {n for n in unbound if n and f"_{n}" in declared and n not in declared}
    if not text or not targets:
        return text, set()

    pattern = re.compile(r"(?<![\w.])(" + "|".join(sorted(map(re.escape, targets),
                                                         key=len, reverse=True))
                         + r")\b")
    rebound: set[str] = set()

    def _fix_code(chunk: str) -> str:
        def _sub(m: "re.Match") -> str:
            rebound.add(m.group(1))
            return "_" + m.group(1)
        return pattern.sub(_sub, chunk)

    out: list[str] = []
    pos = 0
    for m in _ALIAS_SKIP_RE.finditer(text):
        out.append(_fix_code(text[pos:m.start()]))
        out.append(m.group(0))          # strings/comments pass through verbatim
        pos = m.end()
    out.append(_fix_code(text[pos:]))
    return "".join(out), rebound


def lint_unbound_identifiers(text: str, *, known: "set[str] | frozenset[str]",
                             arities: "dict[str, tuple[int, int]] | None" = None,
                             params: "dict[str, tuple[str, ...]] | None" = None,
                             signature: str = "",
                             field_types: "dict[str, str] | None" = None
                             ) -> "tuple[str, set[str]]":
    """Neutralize statements referencing an identifier that cannot bind.

    Returns `(text, flagged_names)`. Same three-way strategy as every other
    `lint_*` pass here: comment out a complete self-terminating statement,
    swap an `if`/`while` condition for `false` when the statement spans lines,
    and leave a line alone entirely when neither is safe — with the two
    structure-preserving cases above, because a repair that trades CS0103 for a
    parse error has not repaired anything.
    """
    bad = find_unbound_identifiers(text, known=known, arities=arities,
                                   params=params, signature=signature,
                                   field_types=field_types)
    if not bad:
        return text, set()
    return _neutralize_flagged_lines(text, bad)


# ── Template-field type misuse (CS1061 / CS1503) ─────────────────────────────
# The unbound-identifier pass above proves an identifier BINDS; it never asks
# what binding to it actually means. `_music.PickClip(...)` binds — `_music`
# is a real template field — and ships anyway, because `_music`'s own
# declared type (AudioSource) is never consulted. Same failure shape as the
# GTA member checker earlier in this module (`find_invalid_member_refs`), on
# the Unity side: an authored fragment invents a member call or an assignment
# the RECEIVER's own declared type cannot support. Hit live on a compose
# build (AutomatonCitadel), three separate micros, all the same shape:
#
#   AudioManager.cs(119)  _sparkSource.Play(_music.PickClip("brass_spark"))
#       — `_music` is the template's OWN `private AudioSource _music;`;
#       `PickClip()` is a real, zero-argument method — on `AudioCue`, an
#       unrelated class the same template folder also declares. CS1061.
#   PlayerHealth.cs(62)   if (_lastHit != null && _lastHit.CompareTag("Cog"))
#       — `_lastHit` is the template's own invulnerability-timer float, used
#       correctly elsewhere as `Time.time - _lastHit`. CS1061.
#   LightingDirector.cs(242)  ambientGround = Mathf.Lerp(ambientGround, ...);
#       — `ambientGround` is `[SerializeField] private Color ambientGround`.
#       Mathf.Lerp always returns float. CS1503.
#
# Five cases are judged, each requiring the field's type to be read straight
# off the template (never guessed):
#
#   (a) MEMBER CALL on a primitive-typed field. A float/int/bool/... field has
#       no members beyond the handful every value type carries
#       (`_MEMBER_ALWAYS_OK`) plus `CompareTo` — calling anything else on one
#       is unconditionally wrong, whatever the name.
#   (b) MEMBER CALL against a small, hand-verified Unity component surface
#       (`_UNITY_FIELD_TYPE_SURFACE`) — never a guess at the rest of a type's
#       real API. AudioSource/MonoBehaviour are the entries today, each added
#       only once its listed surface was checked against the real Unity API,
#       the same discipline `_MEMBER_VAR_TYPES` follows for GTA types above.
#   (c) ASSIGNMENT of a provably numeric expression into a struct field with
#       no implicit numeric conversion (Color, Vector2/3/4, Quaternion).
#   (d) ARGUMENT to one of Animator's four state-switch methods
#       (`_ANIMATOR_STATE_SWITCH_METHODS`) whose second positional argument is
#       a provably string-typed expression — every real overload of all four
#       takes a number there (duration or layer), never a string, so this
#       needs no knowledge of which overload was intended.
#   (e) BARE PROPERTY ACCESS (`recv.Member`, no call) or INDEXING
#       (`recv[...]`) on a field typed `object`/a primitive/one of the
#       `_UNITY_FIELD_TYPE_SURFACE` types — none of those support a custom
#       property or an indexer, so this reuses the SAME three buckets (a)/(b)
#       already judge for calls, just without requiring the trailing `(`.
#       Confirmed live, DeadReckoningProtocol GameRules.cs(265,266,277,278):
#       `_objectives.Count` / `_objectives[_currentStepIndex]` — `_objectives`
#       is the template's own generic `MonoBehaviour` reflection binding (see
#       Rules.cs.j2's `BindObjectives()`), not a collection. Cases (a)/(b)
#       never saw this because their regex requires a call's `(`.
_NUMERIC_BOOL_TYPES = _PRIMITIVE_TYPES - {"string"}
_STRUCT_NO_NUMERIC_CONVERSION = frozenset({
    "color", "color32", "vector2", "vector3", "vector4", "quaternion",
})
_UNITY_FIELD_TYPE_SURFACE: "dict[str, frozenset[str]]" = {
    "audiosource": frozenset({
        "Play", "PlayOneShot", "PlayDelayed", "PlayScheduled", "Stop", "Pause",
        "UnPause", "Mute", "SetScheduledStartTime", "SetScheduledEndTime",
        "clip", "volume", "pitch", "loop", "mute", "playOnAwake", "isPlaying",
        "time", "timeSamples", "outputAudioMixerGroup", "spatialBlend",
        "spatialize", "priority", "panStereo", "reverbZoneMix", "bypassEffects",
        "bypassListenerEffects", "bypassReverbZones", "dopplerLevel", "spread",
        "minDistance", "maxDistance", "rolloffMode", "ignoreListenerVolume",
        "ignoreListenerPause", "velocityUpdateMode",
    }),
    # `MonoBehaviour` is this pipeline's OWN fallback type for a cross-script
    # reference it cannot resolve to a real generated class (no sanctioned
    # pattern exists for one custom script to reference another — see
    # docs/model-pipeline-architecture.md) — the same shape `object` already
    # covers above, just less restrictive because MonoBehaviour genuinely has
    # a real (small, well-known) API. Confirmed live, DeadReckoningProtocol
    # EngineerEscort.cs(17): `[SerializeField] private MonoBehaviour
    # _engineerDialogueManager;` then `_engineerDialogueManager.
    # TriggerEvent("StartEscortDialogue");` — TriggerEvent is not a member of
    # MonoBehaviour or anything it inherits from (CS1061), but fell through
    # every existing check because MonoBehaviour was in none of the
    # type-specific buckets above. Full public surface of
    # MonoBehaviour/Behaviour/Component/UnityEngine.Object — never a guess.
    "monobehaviour": frozenset({
        "GetComponent", "GetComponents", "GetComponentInChildren",
        "GetComponentsInChildren", "GetComponentInParent",
        "GetComponentsInParent", "TryGetComponent",
        "CompareTag", "SendMessage", "SendMessageUpwards", "BroadcastMessage",
        "StartCoroutine", "StopCoroutine", "StopAllCoroutines",
        "Invoke", "InvokeRepeating", "CancelInvoke", "IsInvoking",
        "GetInstanceID", "print",
        "gameObject", "transform", "tag", "name", "hideFlags",
        "enabled", "isActiveAndEnabled", "useGUILayout",
    }),
}

_FIELD_MEMBER_CALL_RE = re.compile(
    r"(?<![\w.])(?P<recv>[A-Za-z_]\w*)\.(?P<member>[A-Za-z_]\w*)\s*\(")
# (e)'s two shapes: a property READ (no trailing call) and an indexer. Both
# exclude what _FIELD_MEMBER_CALL_RE already owns (a member immediately
# followed by `(`), so the two never double-flag the same text.
_FIELD_PROPERTY_RE = re.compile(
    r"(?<![\w.])(?P<recv>[A-Za-z_]\w*)\.(?P<member>[A-Za-z_]\w*)\b(?!\s*\()")
_FIELD_INDEX_RE = re.compile(r"(?<![\w.])(?P<recv>[A-Za-z_]\w*)\s*\[")
_FIELD_ASSIGN_RE = re.compile(
    r"(?<![\w.])(?P<recv>[A-Za-z_]\w*)[ \t]*"
    r"(?P<op>\+=|-=|\*=|/=|(?<![=!<>])=(?!=))"
    r"[ \t]*(?P<rhs>[^;\n]+);")

# (d) Animator's four state-switch overload families all share one shape:
# (string|int stateName, float duration_or_normalizedTime, int layer = -1,
# ...). None of them EVER take a string as the second positional argument, in
# any real overload — so a statically-string-typed second argument is wrong
# regardless of which overload was intended, no knowledge of the rest of
# Animator's API required. Confirmed live, DeadReckoningProtocol
# PlayerAnimator.cs(109): `_anim.CrossFade(_vehicleModeHash, modeName)` —
# `modeName` is this METHOD'S OWN `string` parameter, passed where the
# transition duration belongs (CS1503 on both arguments, since the first is
# also wrong — an `int` hash where CrossFade's own overloads take `string` or
# `int`, but AS THE FIRST ARG. The second-argument check alone is enough to
# catch this call and every other realistic misuse of this method family).
_ANIMATOR_STATE_SWITCH_METHODS = frozenset({
    "CrossFade", "CrossFadeInFixedTime", "Play", "PlayInFixedTime",
})


def _find_matching_close_paren(text: str, start: int) -> "int | None":
    """Index of the `)` matching an already-consumed `(` (`start` is the
    index right after it), nesting-aware for `()[]{}`. None when the parens
    never balance within a bounded scan — bail out rather than mis-parse; a
    missed check is safe, a false flag on a mis-parsed call is not. Pair with
    the module's OWN `_split_top_level_args(args_text)` (a plain string
    splitter, no position-finding) to get the actual argument list."""
    depth = 1
    limit = min(len(text), start + 2000)
    for i in range(start, limit):
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return i
    return None


def find_field_type_misuse(text: str, *, field_types: "dict[str, str]",
                           signature: str = "") -> "dict[int, set[str]]":
    """`{line_index: {description, ...}}` for statements that use a
    TEMPLATE-declared field in a way its own declared type cannot support —
    see the module comment above for the five cases judged.

    `field_types` maps field name -> declared type, read off the micro's own
    template (`template_field_types`). A name the fragment or its own
    signature redeclares (a local/parameter shadowing the field) is never
    judged here — that binding is not the template's field at all.
    """
    if not text or not field_types:
        return {}
    mask = _code_mask(text)
    shadowed = _fragment_declares(text) | _signature_param_names(signature)
    bad: "dict[int, set[str]]" = {}

    def flag(pos: int, msg: str) -> None:
        bad.setdefault(text.count("\n", 0, pos), set()).add(msg)

    locals_: "dict[str, str] | None" = None
    for m in _FIELD_MEMBER_CALL_RE.finditer(text):
        if not mask[m.start("recv")]:
            continue
        recv, member = m.group("recv"), m.group("member")
        if recv in shadowed or member in _MEMBER_ALWAYS_OK:
            continue
        ftype = field_types.get(recv)
        if ftype is None:
            continue
        base = ftype.lower()
        if base == "object":
            # `object` has no members beyond `_MEMBER_ALWAYS_OK` (already
            # excluded above) — unlike a primitive, not even `CompareTo`.
            # Confirmed live, StealthAudioManager.cs(20,38): a cross-script
            # reference the LLM could not type (no sanctioned pattern exists
            # for one custom script to reference ANOTHER — see
            # docs/model-pipeline-architecture.md) typed as `object` and then
            # called real-sounding methods (`RetractGear`, `OnEscortComplete`)
            # that can never exist on it. CS1061 every time, unconditionally.
            flag(m.start("recv"), f"{recv}.{member}() -- {recv} is object, "
                                  f"which has no such member")
            continue
        if base in _NUMERIC_BOOL_TYPES:
            if member != "CompareTo":
                flag(m.start("recv"), f"{recv}.{member}() -- {recv} is "
                                      f"{ftype}, which has no such member")
            continue
        surface = _UNITY_FIELD_TYPE_SURFACE.get(base)
        if surface is not None and member not in surface:
            flag(m.start("recv"), f"{recv}.{member}() -- {recv} is "
                                  f"{ftype}, which has no such member")
        if base == "animator" and member in _ANIMATOR_STATE_SWITCH_METHODS:
            close = _find_matching_close_paren(text, m.end())
            if close is not None:
                args = _split_top_level_args(text[m.end():close])
                if len(args) >= 2:
                    if locals_ is None:
                        locals_ = _declared_types(signature + "\n" + text)
                    arg2 = _expression_type(args[1].strip(), locals_)
                    if arg2 == "string":
                        flag(m.start("recv"), f"{recv}.{member}(...) -- second "
                                              f"argument must be numeric (a "
                                              f"transition duration or layer "
                                              f"index), got string")
                    # Two more shapes, checked over EVERY argument (not just
                    # arg2): a fabricated static member on the field's own
                    # declared type, or a bare identifier that is not
                    # declared anywhere reachable. Confirmed live
                    # (2026-08-28), PlayerAnimator.cs(102):
                    #     _anim.Play(animName, Animator.DefaultStateMode, __J_Id);
                    # `Animator.DefaultStateMode` is not a real Unity member
                    # at all (the LLM wrote the TYPE name where an instance/
                    # constant belonged); `__J_Id` is never declared anywhere
                    # in the fragment or signature. Neither has a ground
                    # truth to repair TO — arg2's string-vs-numeric check
                    # above can rewrite nothing meaningful here either, since
                    # the values themselves are invented, not just
                    # mistyped — so this flags for neutralization like every
                    # other unrecoverable-reference case in this module,
                    # rather than guessing a replacement value.
                    for arg in args:
                        a = arg.strip()
                        if not a or a == args[1].strip() and arg2 == "string":
                            continue   # already flagged above
                        if a.startswith(ftype + "."):
                            flag(m.start("recv"), f"{recv}.{member}(...) -- "
                                                  f"argument {a!r} is not a real "
                                                  f"{ftype} member")
                            continue
                        if re.fullmatch(r"[A-Za-z_]\w*", a) and a not in _CS_KEYWORDS \
                                and a not in ("null", "true", "false") \
                                and a not in locals_ and a not in field_types:
                            flag(m.start("recv"), f"{recv}.{member}(...) -- "
                                                  f"argument {a!r} is not declared "
                                                  f"anywhere")

    for m in _FIELD_PROPERTY_RE.finditer(text):
        if not mask[m.start("recv")]:
            continue
        recv, member = m.group("recv"), m.group("member")
        if recv in shadowed or member in _MEMBER_ALWAYS_OK:
            continue
        ftype = field_types.get(recv)
        if ftype is None:
            continue
        base = ftype.lower()
        if base == "object":
            flag(m.start("recv"), f"{recv}.{member} -- {recv} is object, "
                                  f"which has no such member")
            continue
        if base in _NUMERIC_BOOL_TYPES:
            if member != "CompareTo":
                flag(m.start("recv"), f"{recv}.{member} -- {recv} is "
                                      f"{ftype}, which has no such member")
            continue
        surface = _UNITY_FIELD_TYPE_SURFACE.get(base)
        if surface is not None and member not in surface:
            flag(m.start("recv"), f"{recv}.{member} -- {recv} is "
                                  f"{ftype}, which has no such member")

    for m in _FIELD_INDEX_RE.finditer(text):
        if not mask[m.start("recv")]:
            continue
        recv = m.group("recv")
        if recv in shadowed:
            continue
        ftype = field_types.get(recv)
        if ftype is None:
            continue
        base = ftype.lower()
        if (base == "object" or base in _NUMERIC_BOOL_TYPES
                or base in _UNITY_FIELD_TYPE_SURFACE):
            flag(m.start("recv"), f"{recv}[...] -- {recv} is {ftype}, "
                                  f"which has no indexer")

    for m in _FIELD_ASSIGN_RE.finditer(text):
        if not mask[m.start("recv")]:
            continue
        recv = m.group("recv")
        if recv in shadowed:
            continue
        ftype = field_types.get(recv)
        if ftype is None or ftype.lower() not in _STRUCT_NO_NUMERIC_CONVERSION:
            continue
        if locals_ is None:
            locals_ = _declared_types(signature + "\n" + text)
        got = _expression_type(m.group("rhs").strip(), locals_)
        if got in ("int", "float", "double", "bool"):
            flag(m.start("recv"), f"{recv} {m.group('op')} ... -- {recv} is "
                                  f"{ftype}, value is {got}")
    return bad


def lint_field_type_misuse(text: str, *, field_types: "dict[str, str]",
                           signature: str = "") -> "tuple[str, set[str]]":
    """Neutralize statements that use a template field in a way its declared
    type cannot support. Same three-way repair strategy as every other
    `lint_*` pass here — see `_neutralize_flagged_lines`."""
    bad = find_field_type_misuse(text, field_types=field_types, signature=signature)
    if not bad:
        return text, set()
    return _neutralize_flagged_lines(text, bad, reason="type-incompatible use(s)")


# CS0200/CS1612 — assignment (direct, compound, or through a value-type
# sub-member like `.y`) into a Unity component property that is get-only.
# Confirmed live, DropZone's PlayerController.cs(238,281,283):
#
#     _cc.velocity = vel;
#     _cc.velocity.y -= _gravityScale * Time.fixedDeltaTime;
#     _cc.velocity += drag * Time.fixedDeltaTime;
#
# — three shapes of the same API confusion in one file (two in `update_extra`,
# one in an `extra_methods` body). `CharacterController.velocity` is
# `{ get; }` only — the controller computes it internally from its own
# collision-aware `.Move()` calls — unlike `Rigidbody.velocity`, which
# genuinely is settable and which the model reliably conflates it with
# whenever it wants to cap speed or apply drag.
#
# `unity_api_surface.py` (extracted from the installed Editor's XML docs)
# proves a member EXISTS on a type, not whether it has a setter, so it cannot
# tell these apart from `lint_invalid_member_uses` above — this is a second,
# narrower question with no data source but a human reading the real API, the
# same discipline `_UNITY_FIELD_TYPE_SURFACE` already follows: small,
# hand-verified, grown only as a new type is actually confirmed live rather
# than guessed at. No safe rewrite exists either way: the model's real intent
# (a speed cap, a drag force) has no ground-truth replacement expression here
# — the correct fix is `_cc.Move(...)` with a DISPLACEMENT the model never
# computed, not a velocity write — so, like every other invented-member case
# in this module, this is judged for neutralization rather than guessed at.
_UNITY_READONLY_PROPERTIES: "dict[str, frozenset[str]]" = {
    "charactercontroller": frozenset({"velocity", "isGrounded", "collisionFlags"}),
}
_READONLY_PROPERTY_ASSIGN_RE = re.compile(
    r"(?<![\w.])(?P<recv>[A-Za-z_]\w*)\.(?P<member>[A-Za-z_]\w*)"
    r"(?:\.[A-Za-z_]\w*)?"
    r"[ \t]*(?:\+=|-=|\*=|/=|(?<![=!<>])=(?!=))")


def find_readonly_property_assign_misuse(text: str, *, field_types: "dict[str, str]"
                                         ) -> "dict[int, set[str]]":
    """`{line_index: {"recv.member", ...}}` for an assignment into a Unity
    component property known to be get-only, reached directly, compounded, or
    through a value-type sub-member (`.velocity.y -= ...`). `field_types` is
    the same `template_field_types()` ground truth `find_field_type_misuse`
    uses — a name the fragment declares itself is never the template's field
    at all, but shadowing does not apply here the way it does there: this
    checks a FIXED external API surface (`_UNITY_READONLY_PROPERTIES`), not
    the template's own members, so there is nothing for a local re-declare of
    `recv` to legitimately shadow.
    """
    if not text or not field_types:
        return {}
    mask = _code_mask(text)
    bad: "dict[int, set[str]]" = {}
    for m in _READONLY_PROPERTY_ASSIGN_RE.finditer(text):
        if not mask[m.start("recv")]:
            continue
        recv, member = m.group("recv"), m.group("member")
        ftype = field_types.get(recv)
        if ftype is None:
            continue
        readonly = _UNITY_READONLY_PROPERTIES.get(ftype.lower())
        if readonly is None or member not in readonly:
            continue
        line = text.count("\n", 0, m.start("recv"))
        bad.setdefault(line, set()).add(f"{recv}.{member}")
    return bad


def lint_readonly_property_assign_misuse(text: str, *, field_types: "dict[str, str]"
                                         ) -> "tuple[str, set[str]]":
    """Neutralize an assignment into a known get-only Unity component
    property. Same three-way repair as every other `lint_*` pass here."""
    bad = find_readonly_property_assign_misuse(text, field_types=field_types)
    if not bad:
        return text, set()
    return _neutralize_flagged_lines(
        text, bad, reason="assignment(s) to a get-only Unity property")


# ── Invented members on real Unity types (CS0117 / CS1061) ───────────────────
#
# The pass above proves a field is not used as the wrong KIND of thing; this
# one asks the next question a compiler asks: does the member actually exist on
# that type? Live in one build:
#
#     CS0117  'Material' does not contain a definition for 'mainTextureMode'
#     CS1061  'Touchscreen' does not contain a definition for ...
#
# The GTA backend has had this check since its own corpus study
# (`lint_member_references` against `lookups/gta_api_surface.json`); Unity had
# no equivalent because nothing supplied the surface. `src/unity_api_surface.py`
# now does, extracted from the installed Editor's own XML documentation.
#
# Unlike the GTA checker, which must INFER a variable's type from declaration
# patterns, this one is keyed off types the schema already DECLARES — the same
# `field_types` map `find_field_type_misuse` above consumes. A field whose type
# is unknown, ambiguous, or too thinly documented to judge is skipped outright:
# the pass abstains rather than guesses, so a missing surface changes nothing.
_MEMBER_ACCESS_TAIL = r"\s*\.\s*([A-Za-z_]\w*)"


def find_invalid_member_uses(text: str, *, field_types: "dict[str, str]",
                             surface: "dict | None") -> "dict[int, set[str]]":
    """`{line_index: {"Type.member", ...}}` for members that do not exist on
    the declared type of the field they are reached through.

    `surface=None` (never built) or an unknown/ambiguous type is a no-op.
    """
    if not text or not field_types or not surface:
        return {}
    from ..unity_api_surface import members_for, resolve_type

    bad: "dict[int, set[str]]" = {}
    for fname, declared in field_types.items():
        if not fname or not declared:
            continue
        fqn = resolve_type(str(declared), surface)
        if not fqn:
            continue                       # unknown or ambiguous type — no opinion
        valid = members_for(fqn, surface)
        if valid is None:
            continue                       # extraction too thin to judge against
        pattern = re.compile(r"(?<![\w.])" + re.escape(fname) + _MEMBER_ACCESS_TAIL)
        for m in pattern.finditer(text):
            member = m.group(1)
            if member in valid:
                continue
            line = text.count("\n", 0, m.start(1))
            bad.setdefault(line, set()).add(f"{fqn.rsplit('.', 1)[-1]}.{member}")
    return bad


def lint_invalid_member_uses(text: str, *, field_types: "dict[str, str]",
                             surface: "dict | None"
                             ) -> "tuple[str, set[str]]":
    """Neutralize statements reaching for a member that does not exist on the
    field's declared type. Same three-way repair as every other `lint_*` pass
    here — never guesses a replacement member, because the right one is not
    recoverable from a wrong one."""
    bad = find_invalid_member_uses(text, field_types=field_types, surface=surface)
    if not bad:
        return text, set()
    return _neutralize_flagged_lines(
        text, bad, reason="member(s) that do not exist on the declared type")


# CS1503/CS0019 — a field the template declares NON-vector (a `string` in the
# confirmed case) used where a Vector3 belongs: an argument to one of
# Vector3's static methods, or an operand of `+`/`-` with another Vector3.
# Confirmed live, DropZone's WorldInteractable.cs(65):
#
#     float dist = Vector3.Distance(transform.position, playerTag);
#     transform.position += (playerTag - transform.position).normalized * ...;
#
# `playerTag` is `Interactable.cs.j2`'s own `[SerializeField] private string
# playerTag` (used correctly elsewhere with `CompareTag`) — the model needed
# a reference to the player's TRANSFORM to pull the pickup toward it and
# reused the wrong existing field instead of declaring a new one. There is no
# safe rewrite: no field holding a player Transform exists anywhere in this
# fragment or the template, so — like `CharacterController.velocity` above —
# this is judged for neutralization rather than guessed at.
_VECTOR3_STATIC_METHODS = frozenset({
    "Distance", "Angle", "SignedAngle", "Dot", "Cross", "Project",
    "ProjectOnPlane", "Reflect", "Lerp", "LerpUnclamped", "Slerp",
    "SlerpUnclamped", "MoveTowards", "RotateTowards", "Min", "Max",
    "ClampMagnitude", "Scale",
})
_VECTOR3_STATIC_CALL_RE = re.compile(
    r"\bVector3\.(?P<method>[A-Za-z_]\w*)\s*\(")
_BARE_PLUSMINUS_RE = re.compile(
    r"(?<![\w.])(?P<lhs>[A-Za-z_]\w*)[ \t]*(?P<op>[+-])[ \t]*"
    r"(?P<rhs>[A-Za-z_]\w*)(?![\w.(])")
# A field is not "Vector3" itself here (only NON-vector types are judged
# against a Vector3-arithmetic context) — anything the struct-numeric-
# conversion check above already owns is excluded so the two never overlap.
_NON_VECTOR3_JUDGED_TYPES = _NUMERIC_BOOL_TYPES | {"string"}


def find_field_vector3_context_misuse(text: str, *, field_types: "dict[str, str]"
                                      ) -> "dict[int, set[str]]":
    """`{line_index: {"fieldName", ...}}` for a field known NOT to be a
    Vector3 used where the surrounding code demands one: a positional
    argument to a Vector3 static method, or a bare operand of `+`/`-` beside
    another bare identifier. `field_types` is the same `template_field_types()`
    ground truth `find_field_type_misuse` uses."""
    if not text or not field_types:
        return {}
    mask = _code_mask(text)
    bad: "dict[int, set[str]]" = {}

    def flag(pos: int, name: str) -> None:
        bad.setdefault(text.count("\n", 0, pos), set()).add(name)

    for m in _VECTOR3_STATIC_CALL_RE.finditer(text):
        if not mask[m.start()] or m.group("method") not in _VECTOR3_STATIC_METHODS:
            continue
        close = _find_matching_close_paren(text, m.end())
        if close is None:
            continue
        args_start = m.end()
        for arg in _split_top_level_args(text[args_start:close]):
            a = arg.strip()
            ftype = field_types.get(a)
            if ftype is not None and ftype.lower() in _NON_VECTOR3_JUDGED_TYPES:
                flag(args_start, a)

    for m in _BARE_PLUSMINUS_RE.finditer(text):
        if not mask[m.start("lhs")]:
            continue
        lhs, rhs = m.group("lhs"), m.group("rhs")
        for name in (lhs, rhs):
            ftype = field_types.get(name)
            if ftype is not None and ftype.lower() in _NON_VECTOR3_JUDGED_TYPES:
                flag(m.start("lhs"), name)
    return bad


def lint_field_vector3_context_misuse(text: str, *, field_types: "dict[str, str]"
                                      ) -> "tuple[str, set[str]]":
    """Neutralize a non-Vector3 field used in a Vector3-arithmetic context.
    Same three-way repair as every other `lint_*` pass here."""
    bad = find_field_vector3_context_misuse(text, field_types=field_types)
    if not bad:
        return text, set()
    return _neutralize_flagged_lines(
        text, bad, reason="non-Vector3 field used in Vector3 arithmetic")


def _body_is_live(body: str) -> bool:
    """True if a method body still has a statement (not only AUDIT comments)."""
    return any(ln.strip() and not ln.lstrip().startswith("//")
               for ln in (body or "").splitlines())


# ── Tier 2: BUILD what the author reached for ────────────────────────────────
# An identifier the author uses but never declares is not an error. It is the
# author telling us the class needs a piece of state. Deleting the statement
# deletes the mechanic; declaring the field keeps it.
#
# Live on the TowerControl run, this exact shape cost the game its two most
# visible systems:
#
#   neutralized 2 unbindable reference(s): conflictActive, radarSweepColor
#   dropped 2 authored method(s) left with no live statement:
#       GetRadarSweepColor, SetConflictAlert
#
# The methods did not fail. They were emptied by the removal of two lines that
# assigned two fields nobody had declared — the conflict alert and the radar
# sweep, in a game whose whole pressure mechanic is the conflict alert.
#
# The type is inferred ONLY from an unambiguous assignment in the same fragment.
# No guess, no default: an identifier whose type cannot be read off a literal is
# left alone for the existing pass to handle, because a field of the wrong type
# fails louder and later than one that was never declared.
_ASSIGN_LITERAL_RES: tuple[tuple[str, str], ...] = (
    (r'=\s*(?:true|false)\s*[;,)]',                     "bool"),
    (r'=\s*"[^"]*"\s*[;,)]',                            "string"),
    (r"=\s*'[^']'\s*[;,)]",                             "char"),
    (r'=\s*-?\d+\.\d*f\s*[;,)]',                        "float"),
    (r'=\s*-?\d+f\s*[;,)]',                             "float"),
    (r'=\s*-?\d+\.\d+\s*[;,)]',                         "float"),
    (r'=\s*-?\d+\s*[;,)]',                              "int"),
    (r'=\s*new\s+Color\s*\(',                           "Color"),
    (r'=\s*new\s+Vector3\s*\(',                         "Vector3"),
    (r'=\s*new\s+Vector2\s*\(',                         "Vector2"),
    (r'=\s*Color\.\w+\s*[;,)]',                         "Color"),
    (r'=\s*Vector3\.\w+\s*[;,)]',                       "Vector3"),
    (r'=\s*Vector2\.\w+\s*[;,)]',                       "Vector2"),
    (r'=\s*Quaternion\.\w+\s*[;,)]',                    "Quaternion"),
    (r'=\s*Time\.(?:time|deltaTime|fixedDeltaTime)\b',  "float"),
    (r'=\s*new\s+List<([^>]+)>\s*\(',                   "List<{0}>"),
)
# `if (x)` / `while (x)` / `!x` / `x &&` read as a bool even with no assignment.
_BOOL_USE_RE_TMPL = (r'(?:if|while)\s*\(\s*!?\s*{n}\s*[)&|]'
                     r'|!\s*{n}\b'
                     r'|\b{n}\s*(?:&&|\|\|)')


# Types a ternary or a return can name on both sides. Restricted to the value
# types whose static members are unambiguous — `Color.yellow : Color.green` is
# a Color and nothing else. A wrong type is worse than no declaration, so this
# never guesses from a single branch.
_INFERABLE_TYPES = ("Color", "Vector3", "Vector2", "Quaternion")


def _infer_field_type(name: str, blob: str, *,
                      return_types: "dict[str, str] | None" = None) -> Optional[str]:
    """The type `name` must have, read off how it is used — or None."""
    esc = re.escape(name)
    for pattern, ctype in _ASSIGN_LITERAL_RES:
        m = re.search(rf'\b{esc}\s*{pattern}', blob)
        if m:
            return ctype.format(*m.groups()) if "{0}" in ctype else ctype

    # `x = cond ? Color.yellow : Color.green;` — both arms name the same type.
    # This is the shape TowerControl's radar sweep used, and matching only bare
    # literals missed it, so the sweep was deleted along with its two methods.
    for t in _INFERABLE_TYPES:
        if re.search(rf'\b{esc}\s*=\s*[^;]*\?\s*{t}\.\w+\s*:\s*{t}\.\w+', blob):
            return t

    # `return x;` inside a method whose return type is known.
    if return_types and name in return_types:
        return return_types[name]

    if re.search(_BOOL_USE_RE_TMPL.format(n=esc), blob):
        return "bool"
    return None


def _returned_identifier_types(schema) -> "dict[str, str]":
    """`{identifier: type}` for bare `return <ident>;` in a typed method.

    The strongest inference available and the cheapest: a method declared
    `public Color GetRadarSweepColor()` that ends `return radarSweepColor;`
    has told us exactly what that field is.
    """
    out: "dict[str, str]" = {}
    for m in (getattr(schema, "extra_methods", None) or []):
        sig = str(getattr(m, "signature", "") or "")
        rt = _RETURN_TYPE_RE.match(sig)
        if not rt:
            continue
        ctype = rt.group("type")
        if ctype in _VOID_RETURNS:
            continue
        for ident in re.findall(r'\breturn\s+([A-Za-z_]\w*)\s*;',
                                str(getattr(m, "body", "") or "")):
            out.setdefault(ident, ctype)
    return out


_DEFAULT_FOR_TYPE = {
    "bool": "false", "int": "0", "float": "0f", "string": '""',
    "char": "' '", "Color": "Color.white", "Vector3": "Vector3.zero",
    "Vector2": "Vector2.zero", "Quaternion": "Quaternion.identity",
}


def build_missing_fields(schema, unbound: "set[str]", blob: str) -> list[str]:
    """Declare the fields the author used but never declared. Mutates `schema`.

    Returns one message per field built. Only ever ADDS — an identifier whose
    type cannot be inferred is left in `unbound` for the caller to handle as
    before, so this can never make a build worse than it was.
    """
    fields = getattr(schema, "extra_fields", None)
    if fields is None:
        return []
    existing = {str(getattr(f, "name", "")) for f in fields}
    returns = _returned_identifier_types(schema)
    built: list[str] = []
    for name in sorted(unbound):
        # Only plain identifiers: a call (`Foo()`), a member access or an
        # arity complaint is not a field, and the diagnostics carry parentheses.
        if not re.fullmatch(r"[A-Za-z_]\w*", name) or name in existing:
            continue
        ctype = _infer_field_type(name, blob, return_types=returns)
        if ctype is None:
            continue
        try:
            fields.append(CodeField(name=name, type=ctype,
                                    default=_DEFAULT_FOR_TYPE.get(ctype, "")))
        except Exception:                    # noqa: BLE001 — never block a build
            continue
        existing.add(name)
        built.append(f"{ctype} {name}")
    return built


# Return types that need no `return` statement.
_VOID_RETURNS: frozenset[str] = frozenset({"void", "IEnumerator", "async"})
# `<modifiers> <ReturnType> Name(` — the return type is the token before the
# method name. Generic returns (`List<T>`) are matched whole.
_RETURN_TYPE_RE = re.compile(
    r'^\s*(?:(?:public|private|protected|internal|static|virtual|override|'
    r'sealed|async|new|partial|extern|unsafe)\s+)*'
    r'(?P<type>[A-Za-z_][A-Za-z0-9_.]*(?:<[^>()]*>)?(?:\[\])?)\s+'
    r'[A-Za-z_][A-Za-z0-9_]*\s*\(')


def _returns_a_value(signature: str) -> bool:
    """Does this method signature promise a value back?"""
    m = _RETURN_TYPE_RE.match(signature or "")
    return m is not None and m.group("type") not in _VOID_RETURNS


def _ends_in_return(body: str) -> bool:
    """Is the last LIVE statement a return/throw — i.e. does the body already
    guarantee a value on the way out?

    Deliberately shallow. Proper reachability analysis is a compiler's job;
    what this has to catch is the one shape the neutralizer creates, where the
    trailing statements were commented out and what is left simply runs off the
    end of the method.
    """
    live = [ln.strip() for ln in (body or "").splitlines()
            if ln.strip() and not ln.lstrip().startswith("//")]
    if not live:
        return False
    last = live[-1].rstrip("}").strip() or (live[-2].strip() if len(live) > 1 else "")
    return last.startswith("return") or last.startswith("throw")


def _ensure_returns(method):
    """Give a neutralized non-void method a return, or None if it needs none.

    CS0161. The binding pass comments out every statement carrying a reference
    that cannot bind, and a method whose ONLY unconditional `return` was one of
    them still passes `_body_is_live` on its surviving guard clause — so it is
    kept, and it no longer returns anything. Hit live on the TowerControl run,
    SaveSystem.cs(71,18):

        private bool ValidateJson(string content)
        {
            if (string.IsNullOrEmpty(content)) return false;
            // AUDIT: removed unbindable reference(s) System -- ...
            // AUDIT: removed unbindable reference(s) JsonDocument -- ...
            // AUDIT: removed unbindable reference(s) jsonDoc -- return jsonDoc...
        }

    `default` is the honest value to append: the real answer was in the
    statements above it, the AUDIT comments say so in place, and for the
    validators and predicates this fires on it is also the conservative one.
    Dropping the method instead would discard the guard clause that survived
    AND orphan the call sites — the loop handles orphans, but throwing away
    working code to avoid writing one line is the worse trade.
    """
    sig = getattr(method, "signature", "") or ""
    body = getattr(method, "body", "") or ""
    if not _returns_a_value(sig) or _ends_in_return(body):
        return None
    patched = body.rstrip() + (
        "\n        // AUDIT: neutralized statements left no return — "
        "default so the method compiles\n        return default;")
    return (method.model_copy(update={"body": patched})
            if hasattr(method, "model_copy") else None)


# A neutralized statement can orphan the NEXT one: commenting out
# `float lateralSpeed = rb.velocity.x;` leaves `lateralSpeed` undeclared for the
# line below it (live, PlayerController.cs), and dropping a method whose body
# went empty orphans its call sites. So the pass runs to a fixpoint, with the
# scope recomputed each round. Bounded — a round that changes nothing stops it,
# and the ceiling only exists so a pathological fragment cannot spin.
_MAX_BIND_ROUNDS = 8


def bind_authored_code(schema, template_dir) -> list[str]:
    """Neutralize every authored statement that cannot bind against its own
    micro's class. Mutates `schema` in place; returns fixer-log messages."""
    tdir = str(template_dir)
    if not template_members(tdir, str(getattr(schema, "variant", "") or "")):
        return []                          # no parseable template — abstain
    # A template's sibling types are NAMED after the schema — saveload declares
    # `class {{ s.name }}Data`, which neutralises to the placeholder `_J_Data`
    # and matches nothing an authored fragment could write. Expanding it back
    # with the schema's own name is what makes `new SaveSystemData()` bind.
    own = str(getattr(schema, "name", "") or "")
    # The variant AND the schema's other enums decide which branches of the
    # template actually render, and therefore which of its members this file
    # will really have. Reading the variant alone let `spawnPoints` — declared
    # only under `{% if s.spawn_shape == "spawn_points" %}` — count as in scope
    # for a `point` spawner, and the authored methods indexing it shipped.
    var = str(getattr(schema, "variant", "") or "")
    facts = render_facts(schema)
    types = {t.replace(_JINJA_PLACEHOLDER, own) if own else t
             for t in template_types(tdir, var, facts)}
    base = (set(template_scope_members(tdir, var, facts)) | types
            | _MONO_INHERITED | _KNOWN_SYMBOLS)
    base.discard(_JINJA_PLACEHOLDER)
    arities = template_method_arities(tdir, var, facts)
    params  = template_method_params(tdir, var, facts)
    field_types = template_field_types(tdir, var, facts)
    readonly_backing = template_readonly_property_backing(tdir, var, facts)
    readonly_names = template_readonly_property_names(tdir, var, facts)
    void_methods = template_void_methods(tdir, var, facts)
    # Invented members on real engine types (CS0117/CS1061) — the one
    # question none of the passes above ask. None when the surface has
    # never been built, which every consumer treats as no opinion.
    from ..unity_api_surface import default_api_surface
    api_surface = default_api_surface()
    # `find_unbound_identifiers`'s argument-TYPE check (its own `field_types`
    # param) additionally needs the SCHEMA'S OWN authored fields — a field the
    # LLM itself declared (`extra_fields`) is USED, never declared, inside the
    # one fragment being scanned, so `_declared_types` alone never sees it
    # either. Kept separate from `field_types` above (which `lint_field_type_
    # misuse` uses unchanged) so this stays scoped to argument checking only.
    # `template_field_types` is `@lru_cache`d — copy before extending it.
    arg_field_types = dict(field_types)
    for f in (getattr(schema, "extra_fields", None) or []):
        fname = str(getattr(f, "name", "") or "")
        ftype = _field_type_norm(str(getattr(f, "type", "") or ""))
        if fname and ftype:
            arg_field_types.setdefault(fname, ftype)
    flagged: set[str] = set()
    dropped: list[str] = []
    returned: list[str] = []
    built: list[str] = []

    for _ in range(_MAX_BIND_ROUNDS):
        methods = [m for m in (getattr(schema, "extra_methods", None) or [])]
        known = set(base)
        for attr in ("extra_fields", "data_fields"):
            for f in (getattr(schema, attr, None) or []):
                known.add(str(getattr(f, "name", "") or ""))
        for m in methods:
            known.add(_method_name(getattr(m, "signature", "")))
        # An authored method shadows the template's arity — its own signature is
        # what a call to it must match, and this pass does not re-derive that.
        live_arities = {k: v for k, v in arities.items()
                        if k not in {_method_name(m.signature) for m in methods}}

        # TIER 2 FIRST — build before you cut. Anything the author used but
        # never declared gets DECLARED, so the statements using it stay live
        # and the mechanic survives. Only then does the neutralizer run, on
        # whatever genuinely cannot be resolved.
        blob = _authored_blob(schema)
        wanted: set[str] = set()
        for _line, names in find_unbound_identifiers(
                blob, known=known, arities=live_arities, params=params,
                field_types=arg_field_types).items():
            wanted |= names
        made = build_missing_fields(schema, wanted, blob)
        if made:
            built.extend(made)
            known |= {m.split()[-1] for m in made}

        changed = bool(made)

        for key in ("awake_extra", "update_extra", "fixed_update_extra"):
            before = getattr(schema, key, "") or ""
            if not before:
                continue
            fixed_ro = _repair_readonly_property_assign(before, backing=readonly_backing)
            fixed_ro, names3 = lint_readonly_property_misuse(
                fixed_ro, readonly_names=readonly_names)
            after, names = lint_unbound_identifiers(fixed_ro, known=known,
                                                    arities=live_arities,
                                                    params=params,
                                                    field_types=arg_field_types)
            after, names2 = lint_field_type_misuse(after, field_types=field_types)
            after, names4 = lint_invalid_member_uses(
                after, field_types=field_types, surface=api_surface)
            after, names5 = lint_readonly_property_assign_misuse(
                after, field_types=field_types)
            after, names6 = lint_void_method_negated(after, void_methods=void_methods)
            after, names7 = lint_field_vector3_context_misuse(
                after, field_types=field_types)
            names = names | names2 | names3 | names4 | names5 | names6 | names7
            if names or fixed_ro != before:
                setattr(schema, key, after)
                flagged |= names
                changed = True

        surviving: list = []
        for m in methods:
            body = getattr(m, "body", "") or ""
            sig = getattr(m, "signature", "") or ""
            fixed_ro = _repair_readonly_property_assign(body, backing=readonly_backing)
            fixed_ro, names3 = lint_readonly_property_misuse(
                fixed_ro, readonly_names=readonly_names, signature=sig)
            after, names = lint_unbound_identifiers(
                fixed_ro, known=known | _signature_param_names(sig),
                arities=live_arities, params=params, signature=sig,
                field_types=arg_field_types)
            after, names2 = lint_field_type_misuse(after, field_types=field_types,
                                                   signature=sig)
            after, names4 = lint_invalid_member_uses(
                after, field_types=field_types, surface=api_surface)
            after, names5 = lint_readonly_property_assign_misuse(
                after, field_types=field_types)
            after, names6 = lint_void_method_negated(after, void_methods=void_methods)
            after, names7 = lint_field_vector3_context_misuse(
                after, field_types=field_types)
            names = names | names2 | names3 | names4 | names5 | names6 | names7
            if names or fixed_ro != body:
                flagged |= names
                changed = True
                m = m.model_copy(update={"body": after}) if hasattr(m, "model_copy") else m
            if _body_is_live(getattr(m, "body", "")):
                # A body that survived can still have lost the only `return`
                # its non-void signature promised (CS0161).
                repaired = _ensure_returns(m)
                if repaired is not None:
                    m = repaired
                    returned.append(_method_name(getattr(m, "signature", "")))
                    changed = True
                surviving.append(m)
            else:
                name = _method_name(getattr(m, "signature", ""))
                dropped.append(name or "<unnamed>")
                changed = True
        # Unconditionally, not only when one was dropped: a neutralized body is
        # a model_copy, so skipping the write-back would silently discard the
        # repair on every method that survived it.
        if getattr(schema, "extra_methods", None) is not None:
            schema.extra_methods = surviving
        if not changed:
            break

    msgs: list[str] = []
    if flagged:
        msgs.append(f"neutralized {len(flagged)} unbindable/type-incompatible "
                    f"reference(s) in authored C# (CS0103/CS1061/CS7036/CS1503): "
                    f"{', '.join(sorted(flagged))}")
    if dropped:
        msgs.append(f"dropped {len(dropped)} authored method(s) left with no "
                    f"live statement: {', '.join(sorted(set(dropped)))}")
    if built:
        msgs.append(f"BUILT {len(built)} field(s) the author used but did not "
                    f"declare, instead of deleting the statements that use "
                    f"them: {', '.join(sorted(set(built)))}")
    if returned:
        msgs.append(f"appended a default return to {len(returned)} authored "
                    f"method(s) whose only return was neutralized (CS0161): "
                    f"{', '.join(sorted(set(returned)))}")
    return msgs


def harden_against_template(schema, template_dir, *, engine: str = "unity") -> list[str]:
    """Reconcile an authored schema against what its own template emits.

    The last gate before render, and the only one that can see both sides. Runs
    for EVERY micro on both paths that produce one — `MicroPipeline.run` (the
    LLM path) and `UnityCompose._resolve` (the compose path) — so a generator
    that hand-rolls its lint is covered exactly like one that calls
    `lint_extensible`, and a generator added later is covered without being
    asked to remember.

    Drops what would not compile and says which, then heals the usings the
    authored content needs. Never raises: a micro missing one authored member
    still builds, and a duplicate definition takes the whole assembly down.

    `engine` gates the binding pass (`bind_authored_code`), whose symbol
    universe — inherited MonoBehaviour members, the UnityEngine allowlist — is
    Unity's. A GTA/SHVDN micro derives from `Script`, not `MonoBehaviour`, so
    judging its identifiers against Unity's scope would accuse `Tick` and
    `Interval` of not existing. It abstains there rather than guessing; the GTA
    path keeps its own `lint_hash_references`/`lint_member_references` passes.

    `_KNOWN_SYMBOLS` deliberately excludes "GameContext" and every composed
    sibling class name (PlayerHealth, PauseMenu, ...): this pass only ever
    sees ONE micro's own template, never the composed project around it, so it
    has no ground truth for what `GameContext.Instance.Get<T>()` may legally
    resolve to here. That is not a gap to close — `enrich_subsystems`
    (src/unity_micro_code.py, the Step 3.5 prompt for hud/menu/saveload/etc.)
    never tells a micro-generator kind that GameContext exists; that guidance
    (`GAMECONTEXT_SCHEMA_GUIDANCE` in src/unity_prompts.py, and the sibling
    lookup table built by `_script_contract`) is shown only to `type: custom`
    scripts via `enrich_scripts`. So a micro-generator's authored code should
    never legitimately contain a GameContext call, and this pass is correct to
    strip one on sight. If a future prompt change ever offers GameContext to
    micro-generator kinds too, this set — and the composed-project context
    `harden()` would need threaded in to validate the call instead of just
    admitting it — must grow to match.
    """
    reserved = {n.lower() for n in template_members(str(template_dir))}
    msgs: list[str] = []
    if not reserved:
        # No parseable template — abstain rather than "reserve nothing" silently.
        console.print(f"  [yellow]WARN[/] no C# template found under "
                      f"{template_dir!r} — authored members went unchecked "
                      f"against the template (CS0102/CS0111 possible)")
        return msgs

    for attr in ("extra_fields", "data_fields"):
        fields = getattr(schema, attr, None)
        if not isinstance(fields, list) or not fields:
            continue
        kept, dropped = [], []
        for f in fields:
            name = str(getattr(f, "name", "") or "")
            if name.lower() in reserved:
                dropped.append(name)
            else:
                kept.append(f)
        if dropped:
            setattr(schema, attr, kept)
            msgs.append(f"dropped {len(dropped)} authored field(s) the template "
                        f"already declares (CS0102): {', '.join(dropped)}")

    methods = getattr(schema, "extra_methods", None)
    if isinstance(methods, list) and methods:
        kept_m, dropped_m = [], []
        for m in methods:
            name = _method_name(getattr(m, "signature", ""))
            if name and name.lower() in reserved:
                dropped_m.append(name)
            else:
                kept_m.append(m)
        if dropped_m:
            schema.extra_methods = kept_m
            msgs.append(f"dropped {len(dropped_m)} authored method(s) the template "
                        f"already defines (CS0102/CS0111): {', '.join(dropped_m)}")

    # LAST, and after the drops above on purpose: a field this function just
    # removed as a duplicate (CS0102) leaves its every reference unbindable, and
    # only a pass that runs afterwards can see that.
    if engine == "unity":
        msgs += bind_authored_code(schema, template_dir)

    msgs += heal_missing_usings(schema, template_usings=template_usings(str(template_dir)))
    return msgs


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
  A Vector2/Vector3/Vector4/Color/Quaternion default is a constructor call
  with POSITIONAL float args, or the static shorthand — NEVER a {{"key":
  value}} object:
    "new Vector3(0f, 0f, 0f)"   or   "Vector3.zero"        (NOT "{{x: 0f, y: 0f, z: 0f}}")
    "new Color(1f, 1f, 1f, 1f)"  or  "#FFD700"              (Color also takes a hex string)
    "Quaternion.identity"
- An extra_field "default" must be a CONSTANT expression. It is evaluated
  before the object exists, so it can never call GetComponent/
  GetComponentInChildren, read transform or gameObject, or reference another
  extra_field. A component reference starts as "null" and is looked up in
  awake_extra instead:
    {{"name": "rb", "type": "Rigidbody", "default": "null"}}
    "awake_extra": "rb = GetComponent<Rigidbody>();"
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

────────── SCHEMA EXTENSIONS (only if the structural + extra_* fields above genuinely aren't enough) ──────────
If you need a field or method that doesn't fit anywhere above, put it inside
extensions instead of adding a key that doesn't exist on the schema:

  extensions_fields:   new [SerializeField] declarations, same shape as extra_fields
  extensions_methods:  new helper methods, same shape as extra_methods
  extensions_metadata: a flat {{"key": "value"}} object of NON-CODE notes (design
                       rationale, a tuning value you couldn't fit elsewhere) —
                       never C#, never rendered into the script

extensions is sandboxed, not lint-hardened the way extra_* is — a name that
collides with another field anywhere in the schema is dropped rather than
repaired, so give extensions_fields/extensions_methods names that don't
already appear above. Most prompts need none of this; leave all three empty
unless the brief genuinely needs a field the schema has no other place for.
  "extensions_fields": [], "extensions_methods": [], "extensions_metadata": {{}}
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
  A GTA.Math.Vector3 default is a constructor call with POSITIONAL float
  args, never a {{"key": value}} object:
    "new Vector3(0f, 0f, 0f)"   (NOT "{{x: 0f, y: 0f, z: 0f}}")
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
