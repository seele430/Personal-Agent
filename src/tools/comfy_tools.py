"""ComfyUI 图生图工具：把本地 ComfyUI 接进对话。

**GPU 保护（2026-11-08 0x116 VIDEO_TDR_FAILURE 蓝屏后加，别删）**：

- 步数硬上限 30（默认 20）：6 GB 显存跑高步数容易触发 TDR；
- 输入图任一边 > 768 时，工作流里先 `ImageScale` 到 512（读不到尺寸就按大图处理）；
- 调用前读 `/system_stats`，空闲显存 < 2500 MB 直接中文拒绝，**不发 /prompt**；
- 同一进程内禁止并发出图（第二个调用立即拒绝）；
- batch 固定 1（工作流里没有 batch 维度）；超时默认 120 s；
- ComfyUI 必须用 `comfy-agent/scripts/start-comfyui-safe.ps1` 启动
  （`--lowvram --reserve-vram 1`，启动前强制检查空闲显存 ≥ 3 GB）。

环境变量：

- ``PA_COMFY_URL``：默认 ``http://127.0.0.1:8188``
- ``PA_COMFY_TIMEOUT``：轮询上限秒数，默认 120
- ``PA_COMFY_INPUT_DIR``：ComfyUI 的 input 目录（用于读图片尺寸；读不到就按大图缩放）
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time
import urllib.error
import urllib.request

from pathlib import Path
from typing import Any


MAX_STEPS = 30
DEFAULT_TIMEOUT = 120.0
MIN_FREE_VRAM_MB = 2500
MAX_INPUT_SIDE = 768
TARGET_SIDE = 512

_GPU_LOCK = threading.Lock()


COMFY_TOOL = {
    "type": "function",
    "function": {
        "name": "comfy_img2img",
        "description": (
            "用本机 ComfyUI 对一张已有图片做图生图，返回生成图片的文件名。"
            "需要本机 ComfyUI 正在运行；会占用 GPU（低显存模式，单张）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "输入图片在 ComfyUI input 目录下的文件名，如 input.png",
                },
                "prompt": {
                    "type": "string",
                    "description": "正向提示词，英文描述想要的画面",
                },
                "negative": {
                    "type": "string",
                    "description": "负向提示词，默认 blurry, low quality",
                },
                "steps": {
                    "type": "integer",
                    "description": "采样步数 1-30，默认 20（上限 30，防显存超时）",
                },
                "cfg": {"type": "number", "description": "CFG 强度，默认 7.0"},
                "denoise": {"type": "number", "description": "去噪强度 0-1，默认 0.6"},
                "seed": {"type": "integer", "description": "随机种子，默认 0"},
            },
            "required": ["image_path", "prompt"],
            "additionalProperties": False,
        },
    },
}


WORKFLOW_TEMPLATE = {
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 0,
            "steps": 20,
            "cfg": 7.0,
            "sampler_name": "euler",
            "scheduler": "normal",
            "denoise": 0.6,
            "model": ["4", 0],
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
    },
    "4": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "v1-5-pruned-emaonly.safetensors"},
    },
    "5": {"class_type": "VAEEncode", "inputs": {"pixels": ["8", 0], "vae": ["4", 2]}},
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "", "clip": ["4", 1]},
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "", "clip": ["4", 1]},
    },
    "8": {"class_type": "LoadImage", "inputs": {"image": ""}},
    "9": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    "10": {
        "class_type": "SaveImage",
        "inputs": {"filename_prefix": "personal_agent", "images": ["9", 0]},
    },
}


def comfy_url() -> str:
    return os.environ.get("PA_COMFY_URL", "http://127.0.0.1:8188").rstrip("/")


def timeout_seconds() -> float:
    try:
        return float(os.environ.get("PA_COMFY_TIMEOUT", str(DEFAULT_TIMEOUT)))

    except ValueError:
        return DEFAULT_TIMEOUT


def input_dir() -> Path | None:
    raw = os.environ.get("PA_COMFY_INPUT_DIR", "").strip()

    return Path(raw) if raw else None


def _request(method: str, path: str, payload: dict | None = None) -> Any:
    data = None
    headers = {"Accept": "application/json"}

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        f"{comfy_url()}{path}", data=data, method=method, headers=headers
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", "replace")

    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"ComfyUI 返回 HTTP {error.code}：{detail}") from None

    except (urllib.error.URLError, OSError):
        raise RuntimeError(
            f"ComfyUI 未运行：连不上 {comfy_url()}。先启动 ComfyUI 再试。"
        ) from None

    if not raw:
        return {}

    try:
        return json.loads(raw)

    except json.JSONDecodeError:
        raise RuntimeError("ComfyUI 返回的不是 JSON") from None


def free_vram_mb() -> int | None:
    """读 /system_stats 里的空闲显存（MB）。读不到返回 None（不误判）。"""

    stats = _request("GET", "/system_stats")
    devices = stats.get("devices") or []

    for device in devices:
        value = device.get("vram_free")

        if isinstance(value, (int, float)) and value > 0:
            return int(value // (1024 * 1024))

    system = stats.get("system") or {}
    value = system.get("vram_free")

    if isinstance(value, (int, float)) and value > 0:
        return int(value // (1024 * 1024))

    return None


def image_size(path: Path) -> tuple[int, int] | None:
    """读 PNG/JPEG 头部拿尺寸；读不到返回 None（调用方按大图处理）。"""

    try:
        data = path.read_bytes()[:65536]

    except OSError:
        return None

    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])

        return int(width), int(height)

    if data[:2] == b"\xff\xd8":
        index = 2

        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1

                continue

            marker = data[index + 1]

            if marker in (0xC0, 0xC1, 0xC2):
                height, width = struct.unpack(">HH", data[index + 5 : index + 9])

                return int(width), int(height)

            if marker in (0xD8, 0xD9):
                index += 2

                continue

            length = struct.unpack(">H", data[index + 2 : index + 4])[0]
            index += 2 + length

    return None


def needs_downscale(image_name: str) -> bool:
    """任一边 > 768 或尺寸未知 → 需要缩到 512。"""

    base = input_dir()

    if base is None:
        return True

    size = image_size(base / image_name)

    if size is None:
        return True

    return max(size) > MAX_INPUT_SIDE


def build_workflow(
    *,
    image_name: str,
    prompt: str,
    negative: str = "blurry, low quality",
    steps: int = 20,
    cfg: float = 7.0,
    denoise: float = 0.6,
    seed: int = 0,
    downscale: bool = True,
) -> dict:
    if not image_name:
        raise ValueError("image_path 不能为空")

    if not prompt:
        raise ValueError("prompt 不能为空")

    if not 1 <= int(steps) <= MAX_STEPS:
        raise ValueError(f"steps 必须在 1..{MAX_STEPS}（显存保护上限）")

    if not 0.0 < float(denoise) <= 1.0:
        raise ValueError("denoise 必须在 (0, 1]")

    workflow = json.loads(json.dumps(WORKFLOW_TEMPLATE))
    workflow["3"]["inputs"].update(
        {
            "seed": int(seed),
            "steps": int(steps),
            "cfg": float(cfg),
            "denoise": float(denoise),
        }
    )
    workflow["6"]["inputs"]["text"] = prompt
    workflow["7"]["inputs"]["text"] = negative
    workflow["8"]["inputs"]["image"] = image_name

    if downscale:
        workflow["11"] = {
            "class_type": "ImageScale",
            "inputs": {
                "image": ["8", 0],
                "upscale_method": "lanczos",
                "width": TARGET_SIDE,
                "height": TARGET_SIDE,
                "crop": "disabled",
            },
        }
        workflow["5"]["inputs"]["pixels"] = ["11", 0]

    return workflow


def _arguments(tool_call) -> dict:
    raw = getattr(tool_call.function, "arguments", "") or "{}"

    if isinstance(raw, dict):
        return raw

    try:
        parsed = json.loads(raw)

    except json.JSONDecodeError:
        raise ValueError("工具参数不是合法 JSON") from None

    if not isinstance(parsed, dict):
        raise ValueError("工具参数必须是对象")

    return parsed


def comfy_img2img(tool_call, cancel_token=None) -> dict[str, Any]:
    args = _arguments(tool_call)

    if not _GPU_LOCK.acquire(blocking=False):
        raise RuntimeError("已有出图任务在跑，请等它结束（禁止串行连跑）")

    try:
        image_name = str(args.get("image_path") or "").strip()

        workflow = build_workflow(
            image_name=image_name,
            prompt=str(args.get("prompt") or "").strip(),
            negative=str(args.get("negative") or "blurry, low quality"),
            steps=int(args.get("steps") or 20),
            cfg=float(args.get("cfg") or 7.0),
            denoise=float(args.get("denoise") or 0.6),
            seed=int(args.get("seed") or 0),
            downscale=needs_downscale(image_name),
        )

        if cancel_token is not None:
            cancel_token.check()

        free = free_vram_mb()

        if free is not None and free < MIN_FREE_VRAM_MB:
            raise RuntimeError(
                f"显存不足：空闲 {free} MB < {MIN_FREE_VRAM_MB} MB，"
                "先关掉游戏/浏览器或其他占显存的程序再试（不发请求）"
            )

        queued = _request(
            "POST", "/prompt", {"prompt": workflow, "client_id": "personal-agent"}
        )
        prompt_id = queued.get("prompt_id")

        if not isinstance(prompt_id, str) or not prompt_id:
            raise RuntimeError("ComfyUI 没有返回 prompt_id")

        deadline = time.monotonic() + timeout_seconds()

        while time.monotonic() < deadline:
            if cancel_token is not None:
                cancel_token.check()

            history = _request("GET", f"/history/{prompt_id}")
            entry = history.get(prompt_id) or {}
            outputs = entry.get("outputs") or {}
            names = [
                image["filename"]
                for node in outputs.values()
                for image in (node.get("images") or [])
                if image.get("filename")
            ]

            if names:
                return {
                    "success": True,
                    "prompt_id": prompt_id,
                    "images": names,
                    "path": names[0],
                }

            if (entry.get("status") or {}).get("status_str") == "error":
                raise RuntimeError("ComfyUI 执行失败，看看它的控制台输出")

            time.sleep(1.5)

        raise RuntimeError(f"等待出图超时（{timeout_seconds():.0f} 秒）")

    finally:
        _GPU_LOCK.release()


TOOL_DEFINITIONS = (
    (COMFY_TOOL, "ask", comfy_img2img),
)
