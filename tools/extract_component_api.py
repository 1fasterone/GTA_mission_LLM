"""Extract an explicit array of existing Subsystem objects; no prompt selection."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.unity_compose import MicroComposer,Subsystem
from src.unity_component_api import ComponentApiError,extract_components


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--project',default='ComponentApi')
    args=parser.parse_args()
    selected=[Subsystem.model_validate(row) for row in json.loads(args.selection.read_text())]
    try:api=extract_components(MicroComposer(args.project),selected,output_dir=args.output)
    except ComponentApiError as exc:
        print(str(exc),file=sys.stderr);return 1
    print(f"{api.document['format']} {api.fingerprint}: {len(api.document['types'])} project types")
    return 0


if __name__=='__main__':raise SystemExit(main())
