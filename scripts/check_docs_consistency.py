"""文档一致性检查：防数字漂移（README 的计数、GPU 保护文档的常量）。

    .\\.venv/Scripts/python.exe scripts/check_docs_consistency.py

只报告、不自动改文档——检查器自己乱写文档是另一种事故。
"""

from __future__ import annotations

import re
import sys

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools import comfy_tools  # noqa: E402


README = ROOT / "README.md"
GPU_DOC = ROOT / "docs" / "gpu-protection.md"
REGISTRY = ROOT / "tests" / "registry.py"
SPECS = ROOT / "frontend" / "tests"


def registered_files() -> int:
    text = REGISTRY.read_text(encoding="utf-8")

    names = set(re.findall(r'"((?:[a-z]+/)?test_[^"]+\.py)"', text))

    return len(names)


def spec_tests() -> int:
    total = 0

    for path in SPECS.glob("*.spec.ts"):
        total += len(re.findall(r"^\s*test\(", path.read_text(encoding="utf-8"), re.M))

    return total


def main() -> int:
    failures: list[str] = []
    readme = README.read_text(encoding="utf-8")

    # 1) Playwright 条数
    match = re.search(r"Playwright (\d+) 条", readme)
    actual_specs = spec_tests()

    if not match:
        failures.append("Playwright 条数")
        print("FAIL  README 里找不到 Playwright 条数")

    elif int(match.group(1)) != actual_specs:
        failures.append("Playwright 条数")
        print(f"FAIL  Playwright：README {match.group(1)} != 实际 {actual_specs}")

    else:
        print(f"PASS  Playwright = {actual_specs}")

    # 2) GPU 保护常量
    if not GPU_DOC.is_file():
        failures.append("gpu-protection.md 缺失")
        print("FAIL  docs/gpu-protection.md 不存在")

    else:
        doc = GPU_DOC.read_text(encoding="utf-8")
        pairs = (
            ("30", comfy_tools.MAX_STEPS == 30),
            ("2500 MB", comfy_tools.MIN_FREE_VRAM_MB == 2500),
            ("120", comfy_tools.DEFAULT_TIMEOUT == 120.0),
        )

        for label, ok in pairs:
            if not ok:
                failures.append(label)
                print(f"FAIL  代码常量与文档「{label}」不一致")

            elif label not in doc:
                failures.append(label)
                print(f"FAIL  gpu-protection.md 缺少「{label}」")

            else:
                print(f"PASS  GPU 常量「{label}」")

        # 3) 文档提到的启动脚本必须存在
        script = None

        for candidate in (
            Path.home() / "Documents" / "deepseek-harness" / "default-workspace",
            ROOT.parent,
        ):
            target = candidate / "comfy-agent" / "scripts" / "start-comfyui-safe.ps1"

            if target.is_file():
                script = target
                break

        if "start-comfyui-safe.ps1" in doc and (script is None or not script.is_file()):
            failures.append("启动脚本缺失")
            print("FAIL  文档提到 start-comfyui-safe.ps1，但文件不存在")

        elif script is not None and script.is_file():
            print("PASS  安全启动脚本存在")

    print()
    print(f"结果：{'PASS' if not failures else 'FAIL'}（{len(failures)} 处不一致）")

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
