"""单次出图 CLI：一条命令走完"构造参数 → 调用工具 → 打印结果"。

    .\\.venv/Scripts/python.exe scripts/comfy_once.py --prompt "a cat" --image input.png --steps 8

GPU 保护不在这里重复实现：steps 上限、显存预检、并发锁、batch=1 全在
`src.tools.comfy_tools` 里，本脚本只做参数校验与输出。
"""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools import comfy_tools  # noqa: E402


def build_call(args) -> SimpleNamespace:
    arguments = {
        "image_path": args.image,
        "prompt": args.prompt,
        "steps": args.steps,
        "denoise": args.denoise,
        "seed": args.seed,
    }

    return SimpleNamespace(
        function=SimpleNamespace(
            name="comfy_img2img",
            arguments=json.dumps(arguments, ensure_ascii=False),
        )
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="单次 ComfyUI 图生图")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--image", default="input.png")
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--denoise", type=float, default=0.55)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    if not 1 <= args.steps <= comfy_tools.MAX_STEPS:
        print(f"steps 必须在 1..{comfy_tools.MAX_STEPS}（GPU 保护上限），收到 {args.steps}")

        return 2

    if not args.prompt.strip():
        print("prompt 不能为空")

        return 2

    try:
        result = comfy_tools.comfy_img2img(build_call(args))

    except ValueError as error:
        print(f"参数错误：{error}")

        return 2

    except RuntimeError as error:
        print(f"出图失败：{error}")

        return 1

    print(json.dumps(result, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
