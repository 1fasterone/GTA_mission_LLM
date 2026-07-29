"""
rag/_calibrate.py — measure the accept/reject threshold for each gta5 kind.

    python -m rag._calibrate            # table + suggestions
    python -m rag._calibrate --detail   # plus every query's distance

Why this exists: `_KIND_THRESHOLD` in rag/query.py is a set of measured
numbers, not taste. Any change to the corpus (schema modules, scrapers) or to
the embedding model moves the distance distribution, so re-run this after a
rebuild and update the table if the numbers have drifted.

Method: run a set of queries that SHOULD resolve and a set that should not,
then compare the worst true match against the best false one. A kind is
separable when the best false match sits above the worst true one. Where they
overlap, no threshold can divide them and the table says so.
"""
from __future__ import annotations

import sys

# Queries a mission author would plausibly write, one bundle per kind.
GOOD: dict[str, list[str]] = {
    "ped": ["corrupt cop on the take", "swat officer", "biker gang member",
            "beach goer in swimwear", "construction worker", "paramedic",
            "cult member", "prison guard", "Trevor Philips", "a stray dog",
            "businessman in a suit", "security guard", "fireman", "old woman",
            "tourist with a camera", "drug dealer", "farmer", "scuba diver",
            "chef", "pilot", "soldier", "nurse", "janitor", "homeless man",
            "hipster", "golfer", "mechanic"],
    "vehicle": ["police helicopter", "getaway van", "fast supercar", "ambulance",
                "fire truck", "armored money truck", "police car", "dirt bike",
                "fighter jet", "speedboat", "garbage truck", "taxi cab",
                "limousine", "muscle car", "cargo plane", "tank", "bulldozer",
                "school bus", "quad bike", "tow truck"],
    "prop": ["drug package", "duffel bag of cash", "gold bars", "security keycard",
             "laptop computer", "oil barrel", "ammo crate", "briefcase",
             "medical kit", "evidence folder", "shipping container",
             "traffic cone", "gas canister", "wooden pallet", "safe",
             "diamond necklace", "office chair", "dumpster"],
    "weapon": ["baseball bat", "assault rifle", "sniper rifle", "grenade",
               "rocket launcher", "combat knife", "pistol", "shotgun",
               "machine gun", "molotov cocktail", "taser", "machete",
               "submachine gun", "crowbar"],
    "audio": ["police siren wail", "explosion blast", "alarm bell",
              "helicopter rotor noise", "countdown timer beep", "car horn",
              "gunfire", "radio station music", "crowd cheering", "phone ringing"],
    "scenario": ["guard standing watch", "ped smoking a cigarette",
                 "ped talking on the phone", "sunbathing on the beach",
                 "drinking coffee", "jogging in the park", "leaning on a wall",
                 "sitting on a bench", "filming with a phone", "doing yoga"],
    "animation": ["carrying a heavy box", "getting into a car", "aiming a pistol",
                  "dying on the ground", "climbing a ladder", "swimming",
                  "dancing at a club", "surrendering with hands up",
                  "reloading a weapon", "sitting in a chair"],
}

# Fluent English with nothing to do with Los Santos. Every one of these used to
# resolve to a real model under the old flat 0.55 threshold.
NONSENSE: list[str] = [
    "a quadratic equation about tax policy",
    "a medieval sonnet concerning bread",
    "quarterly revenue forecast spreadsheet",
    "photosynthesis in deciduous trees",
    "the rules of competitive origami",
    "database index fragmentation report",
    "mitochondrial DNA sequencing protocol",
    "an argument about continental philosophy",
    "knitting patterns for winter scarves",
    "the history of Byzantine coinage",
    "compiler register allocation strategy",
    "recipe for sourdough starter",
    "orbital mechanics of comets",
    "municipal zoning appeal paperwork",
    "a haiku about disappointment",
]


def main() -> None:
    from rag.query import _KIND_THRESHOLD, RagClient

    detail = "--detail" in sys.argv
    rag = RagClient()
    if not rag.is_ready(domain="gta5"):
        print("gta5 index not built — run: python main.py rag-build")
        raise SystemExit(1)

    def d(query: str, kind: str) -> tuple[float, str]:
        hit = rag.search(query, domain="gta5", kind=kind, n=1)[0]
        return hit["distance"], hit["canonical"]

    print(f"{'kind':<11} {'n':>3} {'worst true':>11} {'best false':>11} {'gap':>7}"
          f" {'current':>8}  verdict")
    print("-" * 76)

    for kind, queries in GOOD.items():
        good = sorted((d(q, kind) for q in queries), key=lambda t: t[0])
        bad  = sorted((d(q, kind) for q in NONSENSE), key=lambda t: t[0])
        worst_true, best_false = good[-1][0], bad[0][0]
        gap     = best_false - worst_true
        current = _KIND_THRESHOLD.get(kind, 0.0)
        rejected = sum(1 for dist, _ in good if dist >= current)
        accepted = sum(1 for dist, _ in bad if dist < current)
        verdict = (f"separable, suggest {worst_true + gap * 0.4:.2f}" if gap > 0
                   else "OVERLAP — no threshold divides these")
        print(f"{kind:<11} {len(good):>3} {worst_true:>11.3f} {best_false:>11.3f} "
              f"{gap:>7.3f} {current:>8.2f}  {verdict}")
        print(f"{'':<11} at current: {rejected}/{len(good)} real rejected, "
              f"{accepted}/{len(bad)} nonsense accepted"
              f"   (worst true: {good[-1][1]})")

        if detail:
            for dist, canonical in good:
                print(f"      true  {dist:.3f}  {canonical}")
            for dist, canonical in bad:
                print(f"      false {dist:.3f}  {canonical}")


if __name__ == "__main__":
    main()
