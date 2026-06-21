"""Quick corpus quality check — run: python rag/_verify.py"""
import json
from pathlib import Path

corpus = Path(__file__).parent / "corpus" / "gta5"
for kind in ("peds", "vehicles", "scenarios", "audio", "animations"):
    path = corpus / f"{kind}.jsonl"
    with path.open(encoding="utf-8") as f:
        entries = [json.loads(line) for line in f if line.strip()]
    e = entries[5]
    print(f"=== {kind} [{len(entries)} total] ===")
    print(f"  id:       {e['id']}")
    print(f"  label:    {e['label']}")
    print(f"  tags:     {e['tags']}")
    print(f"  aliases:  {e.get('aliases', [])[:3]}")
    chunk = e["chunk_text"][:150].replace("\n", " | ")
    print(f"  chunk:    {chunk}")
    print()
