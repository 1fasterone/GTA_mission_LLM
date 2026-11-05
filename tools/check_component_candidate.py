"""Check an independent proposal against an exact saved S1 source snapshot.

Engineer tooling only: this does not invoke the Pipeline LLM or switch production
consumers. The output must be a new evidence directory; failures are never erased.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.unity_component_api import ComponentApi, ComponentApiError, canonical_json, extract_sources
from src.unity_component_check import approve_candidate, check_candidate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, type=Path, help='saved component-api evidence directory')
    parser.add_argument('--candidate', action='append', nargs=2, default=[], metavar=('LOGICAL_PATH', 'LOCAL_FILE'))
    parser.add_argument('--reference', action='append', type=Path, default=[], help='actual assembly, never a stub')
    parser.add_argument('--output', required=True, type=Path, help='new evidence directory for this attempt')
    parser.add_argument('--extend', action='store_true', help='extract approved addition after a clean binding check')
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        parser.error('output contains earlier evidence; choose a new attempt directory')
    try:
        document = json.loads((args.baseline / 'component-api.json').read_text(encoding='utf-8'))
        sources = {}
        for row in document['sources']:
            logical = row['path']
            path = Path(logical)
            if path.is_absolute() or '..' in path.parts or '\\' in logical or ':' in logical:
                raise ValueError(f'invalid baseline logical path: {logical!r}')
            sources[logical] = (args.baseline / 'sources' / path).read_bytes().decode('utf-8')
        # Re-extraction verifies exact source/selection/defines/tool identity,
        # rather than trusting an edited or stale manifest's declared members.
        baseline = extract_sources(sources, selection=document['selection'], defines=document['defines'])
        declarations = dict(document)
        declarations.pop('approved_additions', None)
        if baseline.to_json() != canonical_json(declarations):
            raise ValueError('baseline does not match current sources/toolchain; extract a new baseline')
        # Approval lineage is sidecar evidence rather than another declaration
        # authority. Preserve it after re-extracting the exact source surface.
        baseline = ComponentApi(document, sources)
        candidates = {}
        for logical, local in args.candidate:
            if logical in candidates:
                raise ValueError(f'duplicate candidate logical path: {logical!r}')
            candidates[logical] = Path(local).read_bytes().decode('utf-8')
        if args.extend:
            approved = approve_candidate(baseline, candidates, references=args.reference, output_dir=args.output)
            print(json.dumps({'accepted': True, 'contract': approved.fingerprint, 'unity_editor_execution': 'unverified'}))
            return 0
        result = check_candidate(baseline, candidates, references=args.reference, output_dir=args.output)
        print(json.dumps({'accepted': result.accepted, 'errors': len(result.errors),
                          'unity_editor_execution': 'unverified', 'evidence': str(args.output)}))
        return 0 if result.accepted else 1
    except (OSError, ValueError, KeyError, ComponentApiError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
