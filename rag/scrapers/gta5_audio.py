"""
Fetches multiple DurtyFree audio bank files and writes rag/corpus/gta5/audio.jsonl.

Sources:
  scriptAudioBankNames.json   617 names
  ambientAudioBankNames.json  144 names
  missionAudioBankNames.json   22 names
  musicEventNames.json       2781 names
"""
import sys
from pathlib import Path
import httpx

from ..schema.audio import make_audio_entry

_SOURCES = [
    ("https://raw.githubusercontent.com/DurtyFree/gta-v-data-dumps/master/scriptAudioBankNames.json",
     "scriptAudioBankNames.json"),
    ("https://raw.githubusercontent.com/DurtyFree/gta-v-data-dumps/master/ambientAudioBankNames.json",
     "ambientAudioBankNames.json"),
    ("https://raw.githubusercontent.com/DurtyFree/gta-v-data-dumps/master/missionAudioBankNames.json",
     "missionAudioBankNames.json"),
    ("https://raw.githubusercontent.com/DurtyFree/gta-v-data-dumps/master/musicEventNames.json",
     "musicEventNames.json"),
]

OUT = Path(__file__).parent.parent / "corpus" / "gta5" / "audio.jsonl"


def run(force: bool = False) -> int:
    if OUT.exists() and not force:
        count = OUT.read_text(encoding="utf-8").count("\n")
        print(f"[audio] already present ({count} entries). Use --force to re-scrape.")
        return count

    OUT.parent.mkdir(parents=True, exist_ok=True)
    total = 0

    with OUT.open("w", encoding="utf-8") as f:
        for url, source_file in _SOURCES:
            print(f"[audio] fetching {source_file} ...")
            try:
                with httpx.Client(timeout=60.0, follow_redirects=True) as c:
                    r = c.get(url)
                    r.raise_for_status()
                items = r.json()
                count = 0
                for item in items:
                    name = item if isinstance(item, str) else item.get("Name", "")
                    if name and name.strip():
                        entry = make_audio_entry(name.strip(), source_file)
                        f.write(entry.to_jsonl() + "\n")
                        count += 1
                print(f"[audio]   {count} names from {source_file}")
                total += count
            except Exception as e:
                print(f"[audio] WARN {source_file}: {e}", file=sys.stderr)

    print(f"[audio] {total} total written → {OUT}")
    return total


if __name__ == "__main__":
    run(force="--force" in sys.argv)
