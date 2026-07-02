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
