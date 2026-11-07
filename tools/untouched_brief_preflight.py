"""Read-only preflight on the machine that hosts LM Studio and Unity.

No generation, model inference, asset requests, Unity launch, or license access.
Reports installed Editors and model IDs separately from unverified execution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import urlopen


BASELINE = '6919e7ebb0e7d45a046af6486f700120fbf685a5'
CANDIDATES = (
    'turbo_toys.txt', 'oddities_baggage_claim_3d.txt', 'mossward_2d_topdown.txt',
    'chromabreak_arena_fps.txt', 'tower_control.txt', 'dropzone.txt',
)


def editor_inventory(root: Path) -> list[dict]:
    if not root.is_dir():
        return []
    return [{'directory': child.name, 'executable': str(child / 'Editor/Unity.exe'),
             'executable_present': (child / 'Editor/Unity.exe').is_file()}
            for child in sorted(root.iterdir()) if child.is_dir()]


def input_inventory(repo: Path) -> list[dict]:
    rows = []
    for name in CANDIDATES:
        source = repo / 'prompts' / name
        if not source.is_file():
            rows.append({'path': f'prompts/{name}', 'status': 'missing'})
            continue
        raw = source.read_bytes()
        raw.decode('utf-8')  # Fail visibly rather than silently replace input bytes.
        rows.append({'path': f'prompts/{name}', 'status': 'available',
                     'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw),
                     'encoding': 'utf-8', 'cohort_status': 'candidate_only',
                     'held_out_status': 'not_established'})
    return rows


def model_inventory(base_url: str, expected: str, *, offline: bool = False) -> dict:
    parsed = urlsplit(base_url)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('use an http(s) base URL without credentials, query, or fragment')
    if offline:
        return {'status': 'unverified_offline', 'expected_model': expected}
    try:
        # /models is read-only; loading a model alone does not establish that
        # LM Studio's HTTP server is enabled or reachable from this machine.
        with urlopen(base_url.rstrip('/') + '/models', timeout=10) as response:
            data = json.loads(response.read())
        entries = data.get('data') if isinstance(data, dict) else None
        if not isinstance(entries, list) or any(not isinstance(e, dict) or not isinstance(e.get('id'), str)
                                                for e in entries):
            return {'status': 'invalid_models_response', 'expected_model': expected}
        ids = sorted(e['id'] for e in entries)
        return {'status': 'reachable', 'model_ids': ids, 'expected_model': expected,
                'exact_configured_id_listed': expected in ids,
                'revision_quantization_file_hash': 'unverified', 'inference': 'not_run'}
    except Exception as exc:
        # Exception text can contain credential-bearing remote response bodies;
        # record the failure class only. No secret values are requested or read.
        return {'status': 'unreachable', 'error_type': type(exc).__name__, 'expected_model': expected}


def preflight(repo: Path, unity_root: Path, base_url: str, expected_model: str, *, offline: bool = False) -> dict:
    revision = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    editors = editor_inventory(unity_root)
    dotnet = shutil.which('dotnet')
    sdks = []
    if dotnet:
        result = subprocess.run([dotnet, '--list-sdks'], capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            sdks = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
    model = model_inventory(base_url, expected_model, offline=offline)
    return {
        'format': 'gta-untouched-preflight/v1', 'repository_head': revision.stdout.strip() if revision.returncode == 0 else 'unverified',
        'comparison_baseline': BASELINE, 'platform': sys.platform,
        'unity_root': str(unity_root), 'editors': editors, 'required_ci_editor': '6000.1.4f1',
        'required_editor_present': any(e['directory'] == '6000.1.4f1' and e['executable_present'] for e in editors),
        'installed_sdks': sdks, 'required_sdk': '9.0.312', 'required_sdk_present': '9.0.312' in sdks,
        'model': model, 'original_inputs': input_inventory(repo),
        'cohort_frozen': False, 'requirements_oracle': 'pending', 'live_orchestration': 'pending',
        'unity_compile': 'unverified', 'playmode_effects': 'unverified',
        'limitations': ['Editor file existence is not compilation or licensing verification.',
                        'Listed model IDs do not prove revision, quantization, or successful inference.',
                        'Candidate hashes do not establish original authorship or held-out history.'],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--unity-root', type=Path, default=Path('C:/Program Files/Unity/Hub/Editor'))
    parser.add_argument('--base-url', required=True, help='LM Studio API base URL on this machine, e.g. http://localhost:1234/v1')
    parser.add_argument('--expected-model', default='qwen3.5-9b', help='exact configured model ID, if known')
    parser.add_argument('--offline', action='store_true', help='inventory files without contacting the model server')
    parser.add_argument('--output', type=Path, required=True, help='new JSON report path; never overwrites an earlier attempt')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output exists; use a new attempt path')
    report = preflight(args.repo, args.unity_root, args.base_url, args.expected_model, offline=args.offline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as target:
        json.dump(report, target, ensure_ascii=False, indent=2, sort_keys=True)
        target.write('\n')
    print(json.dumps({'report': str(args.output), 'model': report['model']['status'],
                      'required_editor_present': report['required_editor_present'],
                      'required_sdk_present': report['required_sdk_present'], 'inference': 'not_run'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
