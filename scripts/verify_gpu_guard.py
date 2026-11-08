"""GPU 保护自检（只读，不启动 ComfyUI、不读显存）。

    .\\.venv/Scripts/python.exe scripts/verify_gpu_guard.py

把 2026-11-08 蓝屏后定的四条约束变成可执行检查：常量被改小、脚本里的
`--lowvram` 被删、`finally -Stop` 被拿掉，都会在这里报 FAIL。
"""

from __future__ import annotations

import os
import sys

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools import comfy_tools  # noqa: E402


def _workspace() -> Path | None:
    """按优先级找工作区；找不到返回 None。"""

    candidates = []

    env = os.environ.get("PA_WORKSPACE", "").strip()

    if env:
        candidates.append(Path(env))

    candidates.append(Path.home() / "Documents" / "deepseek-harness" / "default-workspace")
    candidates.append(ROOT.parent)

    for candidate in candidates:
        if (candidate / "comfy-agent" / "scripts").is_dir():
            return candidate

    return None


WORKSPACE = _workspace()
SAFE_SCRIPT = (
    WORKSPACE / "comfy-agent" / "scripts" / "start-comfyui-safe.ps1"
    if WORKSPACE is not None
    else None
)
ONCE_SCRIPT = ROOT / "scripts" / "comfy_once.ps1"

FAILS: list[str] = []
SKIPS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS  {name}")

    else:
        print(f"FAIL  {name} {detail}")
        FAILS.append(name)


def skip(name: str, reason: str) -> None:
    print(f"SKIP  {name}（{reason}）")
    SKIPS.append(name)


def main() -> int:
    # 1) 安全启动脚本
    if SAFE_SCRIPT is None or not SAFE_SCRIPT.is_file():
        check("safe launcher 存在", False, f"找不到 {SAFE_SCRIPT}")
    else:
        text = SAFE_SCRIPT.read_text(encoding="utf-8", errors="replace")

        check("safe launcher: --lowvram", "--lowvram" in text)
        check("safe launcher: --reserve-vram 1", "--reserve-vram" in text and '"1"' in text)
        check("safe launcher: -Stop", "-Stop" in text)
        check("safe launcher: 3 GB 门槛", "3072" in text)

    # 2) Python 常量
    check("steps 上限 = 30", comfy_tools.MAX_STEPS == 30, f"实际 {comfy_tools.MAX_STEPS}")
    check(
        "显存门槛 = 2500 MB",
        comfy_tools.MIN_FREE_VRAM_MB == 2500,
        f"实际 {comfy_tools.MIN_FREE_VRAM_MB}",
    )
    check(
        "默认超时 = 120 s",
        comfy_tools.DEFAULT_TIMEOUT == 120.0,
        f"实际 {comfy_tools.DEFAULT_TIMEOUT}",
    )

    # 3) batch = 1（模板里不该有 batch_size）
    has_batch = any(
        "batch_size" in node.get("inputs", {})
        for node in comfy_tools.WORKFLOW_TEMPLATE.values()
    )
    check("工作流无 batch 维度", not has_batch)

    # 4) 出图脚本必须 finally -Stop
    if not ONCE_SCRIPT.is_file():
        skip("comfy_once.ps1", f"找不到 {ONCE_SCRIPT}")
    else:
        text = ONCE_SCRIPT.read_text(encoding="utf-8", errors="replace")
        check("comfy_once: finally", "finally" in text)
        check("comfy_once: -Stop", "-Stop" in text)
        check("comfy_once: 启动前挡 steps", "steps must be 1..30" in text)

    print()
    print(f"结果：{'PASS' if not FAILS else 'FAIL'}（{len(FAILS)} 项失败，{len(SKIPS)} 项跳过）")

    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
