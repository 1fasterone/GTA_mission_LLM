"""Require evidence that the generated-project Unity compile smoke test ran."""
from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

from tools.ci_prepare_unity_fixture import SMOKE_NAME


def verify_results(directory: Path) -> Path:
    files = sorted(directory.rglob("*.xml")) if directory.is_dir() else []
    for path in files:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        for case in root.iter("test-case"):
            name = case.get("fullname", case.get("name", ""))
            if name.endswith(SMOKE_NAME):
                if case.get("result", "").lower() != "passed":
                    raise RuntimeError(f"Unity compile smoke did not pass: {path}")
                return path
    raise RuntimeError(f"no passing {SMOKE_NAME} test result found in {directory}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(f"verified Unity compile smoke: {verify_results(args.directory)}")


if __name__ == "__main__":
    main()
