"""
Unity API member surface — ground truth for "does this member exist on this
type", the Unity counterpart of `gta_allowlists.load_api_surface`.

WHY THIS EXISTS
The authored-code linters could always prove an identifier BINDS (it is a
declared field, a template member, a method parameter) and, since
`find_field_type_misuse`, that a field is not used as the wrong KIND of thing.
Neither asks the question a C# compiler asks next: does the member being
reached for actually exist on that type? So an authored body calling a
plausible-but-invented member on a real engine type shipped unchallenged and
surfaced only as a Unity console error:

    CS0117  'Material' does not contain a definition for 'mainTextureMode'
    CS1061  'Touchscreen' does not contain a definition for ...

Both hit live in one build. The GTA backend has had this check since its own
corpus study (`lint_member_references` + `lookups/gta_api_surface.json`);
Unity had no equivalent because nothing supplied the surface.

WHERE THE GROUND TRUTH COMES FROM
The installed Unity Editor ships an XML documentation file beside every
managed module DLL (`Editor/Data/Managed/UnityEngine/UnityEngine.*.xml`).
Each documented member is one `<member name="P:UnityEngine.Material.color">`
entry, with methods keyed by signature
(`M:UnityEngine.Material.SetFloat(System.String,System.Single)`). That is a
real, offline, versioned artifact of the engine itself — not a scrape and not
a hand-list.

Frozen-artifact pattern, the same one the Sketchfab catalog uses: extraction
reads a local Unity install (which a given machine may or may not have, and
whose version varies), but its OUTPUT is frozen to
`lookups/unity/api_surface.json` and read back as a plain deterministic
lookup. Steps 4-7 never touch a Unity install.

TWO DELIBERATE LIMITS
1. INHERITANCE. The XML carries no base-class information: `Material.name` is
   documented on `UnityEngine.Object`, not on Material. Rather than guess a
   base chain, every member documented on the universal bases (Object,
   Component, Behaviour, MonoBehaviour) is treated as always-valid on every
   type. That over-permits — nothing will flag `someMaterial.transform` — and
   that is the correct bias: this pass exists to catch INVENTED members with
   no false positives, not to be a type checker.
2. DOCUMENTED MEMBERS ONLY. An undocumented public member would read as
   invented. `_MIN_SURFACE` refuses to judge a type whose extraction came back
   thin, and an unknown type is simply skipped — no opinion, never a guess.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

# `<member name="P:UnityEngine.Material.color">` / `M:...SetFloat(System.String)`
# The capture stops at `(` so a method's parameter signature is dropped: the
# check is "does this NAME exist on the type", never an overload resolution.
_XML_MEMBER_RE = re.compile(r'name="([TPMFE]):([^"(]+)')

# A type whose extracted surface is thinner than this is not judged at all. A
# partial extraction would otherwise flag every legitimate use on it.
_MIN_SURFACE = 5

# Members every reference type carries regardless of what it derives from.
_SYSTEM_OBJECT_MEMBERS = frozenset({
    "ToString", "Equals", "GetHashCode", "GetType", "ReferenceEquals",
})

# The bases whose members are inherited widely enough to treat as universal —
# see limit (1) in the module docstring. Read from the surface itself rather
# than hardcoded, so they track the installed engine version.
_UNIVERSAL_BASES = (
    "UnityEngine.Object",
    "UnityEngine.Component",
    "UnityEngine.Behaviour",
    "UnityEngine.MonoBehaviour",
)

_LOOKUP_RELPATH = Path("unity") / "api_surface.json"


# ── Extraction (reads a local Unity install; output is frozen) ────────────────

def discover_managed_dir() -> Path | None:
    """The newest installed Editor's `Data/Managed/UnityEngine` folder."""
    roots = [
        Path(r"C:/Program Files/Unity/Hub/Editor"),
        Path(r"C:/Program Files/Unity/Editor"),
        Path("/Applications/Unity/Hub/Editor"),
        Path.home() / "Unity" / "Hub" / "Editor",
    ]
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            for rel in ("Editor/Data/Managed/UnityEngine",
                        "Unity.app/Contents/Managed/UnityEngine"):
                cand = child / rel
                if cand.is_dir():
                    found.append(cand)
    if not found:
        return None
    # Newest version string last — the same ordering unity_cli._discover_editors
    # uses for its fallback pick.
    return sorted(found, key=lambda p: p.parts)[-1]


def build_surface(managed_dir: "str | Path") -> dict[str, list[str]]:
    """`{type_fqn: [member, ...]}` parsed from a Unity install's XML docs.

    Deterministic: sorted keys, sorted members, no I/O beyond reading the XML.
    """
    d = Path(managed_dir)
    surface: dict[str, set[str]] = {}
    for xml in sorted(d.glob("UnityEngine*.xml")):
        try:
            text = xml.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for kind, ident in _XML_MEMBER_RE.findall(text):
            if kind == "T" or "." not in ident:
                continue
            owner, _, member = ident.rpartition(".")
            # `#ctor` / `#cctor` are not reachable as `x.Member`.
            if not member or member.startswith("#"):
                continue
            surface.setdefault(owner, set()).add(member)
    return {k: sorted(v) for k, v in sorted(surface.items())}


# ── Frozen lookup (what the pipeline actually reads) ──────────────────────────

def surface_path(lookups_dir: "str | Path" = "lookups") -> Path:
    return Path(lookups_dir) / _LOOKUP_RELPATH


def load_api_surface(lookups_dir: "str | Path" = "lookups") -> "dict | None":
    """The frozen surface, or None when it has never been built.

    None is a first-class answer: every consumer treats a missing surface as
    "no opinion" and changes nothing, exactly as the GTA loader does.
    """
    path = surface_path(lookups_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data.get("types") if "types" in data else data


def write_api_surface(surface: dict, lookups_dir: "str | Path" = "lookups",
                      *, unity_version: str = "") -> Path:
    """Freeze `surface` to the lookup path, stamped with the Editor it came
    from so a stale artifact is legible rather than silent."""
    path = surface_path(lookups_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_source": "Unity Editor XML documentation "
                   "(Editor/Data/Managed/UnityEngine/UnityEngine.*.xml)",
        "_unity_version": unity_version,
        "_type_count": len(surface),
        "types": surface,
    }
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


# ── Resolution helpers used by the linter ─────────────────────────────────────

def _hashable(surface: dict) -> tuple:
    return tuple(sorted(surface))


@lru_cache(maxsize=8)
def _index_for(keys: tuple) -> dict[str, str]:
    """`{simple_name: fqn}` for every UNAMBIGUOUS simple name.

    A name owned by two namespaces (`Cursor` is UnityEngine, UIElements and
    WSA) is dropped rather than guessed — the same tie-abstains rule the
    objective-target and board derivations use. 27 of ~2000 names on
    Unity 6000.1.
    """
    owners: dict[str, set[str]] = {}
    for fqn in keys:
        owners.setdefault(fqn.rsplit(".", 1)[-1], set()).add(fqn)
    return {name: next(iter(fqns)) for name, fqns in owners.items()
            if len(fqns) == 1}


def simple_name_index(surface: dict) -> dict[str, str]:
    return _index_for(_hashable(surface))


@lru_cache(maxsize=8)
def _universal_for(keys: tuple, payload: tuple) -> frozenset:
    members = set(_SYSTEM_OBJECT_MEMBERS)
    lookup = dict(payload)
    for base in _UNIVERSAL_BASES:
        members |= set(lookup.get(base) or ())
    return frozenset(members)


def universal_members(surface: dict) -> frozenset:
    """Members treated as valid on every type — see limit (1) above."""
    payload = tuple((b, tuple(surface.get(b) or ())) for b in _UNIVERSAL_BASES)
    return _universal_for(_hashable(surface), payload)


def resolve_type(declared: str, surface: dict) -> str:
    """A declared C# type -> the surface key for it, or "" when unknown.

    Generics, arrays and nullables are stripped to the element type, because
    `_blocks[i].Foo` and `_block.Foo` ask the same question of the same type.
    """
    t = (declared or "").strip().rstrip("?").strip()
    if not t:
        return ""
    # `List<Material>` / `Material[]` / `Material[,]` -> `Material`
    inner = re.match(r"^(?:[A-Za-z_][\w.]*)\s*<\s*(.+?)\s*>$", t)
    if inner:
        t = inner.group(1).split(",")[-1].strip()
    t = re.sub(r"(?:\s*\[[,\s]*\])+\s*$", "", t).strip()
    if not t:
        return ""
    if t in surface:
        return t
    return simple_name_index(surface).get(t, "")


def members_for(type_fqn: str, surface: dict) -> "frozenset | None":
    """The valid member set for a type, or None to abstain (unknown type, or
    an extraction too thin to judge against)."""
    own = surface.get(type_fqn)
    if not own or len(own) < _MIN_SURFACE:
        return None
    return frozenset(own) | universal_members(surface)


@lru_cache(maxsize=1)
def default_api_surface(lookups_dir: str = "lookups") -> "dict | None":
    """The frozen surface, loaded once per process.

    Cached because the artifact is frozen: re-reading 433 KB of JSON on every
    micro would be pure cost, and the file cannot change mid-run. Rebuild it
    with `python main.py unity-api-surface` when the Editor is upgraded.
    """
    return load_api_surface(lookups_dir)
