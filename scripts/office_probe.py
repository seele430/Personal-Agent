"""只读探测 LibreOffice：路径、版本；不安装、不写系统目录。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.office_convert import find_soffice, soffice_version  # noqa: E402


def main() -> int:
    soffice = find_soffice()
    payload = {
        "available": soffice is not None,
        "path": str(soffice) if soffice else None,
        "version": soffice_version(soffice) if soffice else None,
        "platform": sys.platform,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    return 0 if soffice else 1


if __name__ == "__main__":
    raise SystemExit(main())
