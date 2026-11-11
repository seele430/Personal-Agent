"""本机 CLI：按 artifact_id 转换 Office 文件为 PDF。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.artifacts.registry import ArtifactRegistry  # noqa: E402
from src.core.office_convert import OfficeError, convert_artifact  # noqa: E402
from src.platform.exec_service import Tier1Executor  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="office_convert")
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--owner-id", required=True)
    args = parser.parse_args(argv)

    try:
        result = convert_artifact(
            source_artifact_id=args.artifact_id,
            owner_id=args.owner_id,
            registry=ArtifactRegistry(),
            executor=Tier1Executor(),
        )
    except OfficeError as error:
        print(json.dumps({"error": error.code, "status": error.status}, ensure_ascii=False))
        return 2

    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
