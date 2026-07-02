import json
from pathlib import Path

corpus = Path(__file__).parent / "corpus" / "gta5"

# Build first-match alias maps
ped_map = {}
with (corpus / "peds.jsonl").open(encoding="utf-8") as f:
    for line in f:
        e = json.loads(line)
        for a in e["aliases"]:
            if a.lower() not in ped_map:
                ped_map[a.lower()] = e["canonical"]

veh_map = {}
motos = []
with (corpus / "vehicles.jsonl").open(encoding="utf-8") as f:
    for line in f:
        e = json.loads(line)
        if "class.motorcycle" in e["tags"]:
            motos.append((e["canonical"], "motorcycle" in e["aliases"]))
        for a in e["aliases"]:
            if a.lower() not in veh_map:
                veh_map[a.lower()] = e["canonical"]

print("=== Ped aliases (first-match) ===")
ped_queries = [
    "cop", "prison guard", "cartel guard", "ballas", "cult member",
    "drug dealer", "swat", "dock worker", "scientist", "fbi agent",
    "security guard", "correctional officer",
]
for q in ped_queries:
    r = ped_map.get(q.lower(), "NOT FOUND")
    print(f"  {q:<25} -> {r}")

print()
print("=== Vehicle aliases (first-match) ===")
veh_queries = [
    "motorcycle", "bike", "police car", "suv", "pickup truck",
    "attack helicopter", "fighter jet", "limo", "armored truck", "supercar",
]
for q in veh_queries:
    r = veh_map.get(q.lower(), "NOT FOUND")
    print(f"  {q:<25} -> {r}")

no_moto_alias = [m for m, has in motos if not has]
print(f"\nMotorcycle entries: {len(motos)}, missing 'motorcycle' alias: {len(no_moto_alias)}")
