"""ComfyUI 工具的真实错误路径检查（不需要 GPU / 不需要起服务）。

    .\.venv\Scripts\python.exe scripts\verify_comfy_tool.py

期望：服务未起时，工具抛出中文 RuntimeError（含"未运行"），而不是 traceback 或假成功。
若 8188 上确实有服务在跑，脚本会明确说"跳过"，既不算通过也不算失败。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools import comfy_tools  # noqa: E402


def service_up() -> bool:
    url = comfy_tools.comfy_url()

    try:
        with urllib.request.urlopen(f"{url}/system_stats", timeout=3) as response:
            return response.status == 200

    except (urllib.error.URLError, OSError):
        return False


def main() -> int:
    if service_up():
        print("跳过：8188 上已有 ComfyUI 在运行，错误路径无法验证")

        return 0

    call = SimpleNamespace(
        function=SimpleNamespace(
            name="comfy_img2img",
            arguments=json.dumps({"image_path": "input.png", "prompt": "a cat"}),
        )
    )

    try:
        result = comfy_tools.comfy_img2img(call)

    except RuntimeError as error:
        message = str(error)
        print(f"捕获到预期错误：{message}")

        if "未运行" in message:
            print("PASS：中文提示正确")

            return 0

        print("FAIL：错误信息里没有“未运行”")

        return 1

    print(f"FAIL：服务未起却返回了结果：{result}")

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
