"""ComfyUI 图生图工具：把本地 ComfyUI 接进对话。

设计约束：

- **只做 PA 需要的极小客户端**，与工作区里的 `comfy_agent` 包互不依赖；
- **policy = ask**：会用 GPU、会写文件，默认要人工确认，不让模型自己决定跑图；
- 服务未起 / 超时 / 参数错，一律给中文短句，不把 urllib 的 traceback 丢给模型；
- handler 只在 tier1 runner 子进程里执行（与 sandbox_tools 同一约定），
  因此这里**不**做工作区路径限制——图片由 ComfyUI 自己从它的 input 目录读。

环境变量：

- ``PA_COMFY_URL``：默认 ``http://127.0.0.1:8188``
- ``PA_COMFY_TIMEOUT``：轮询上限秒数，默认 180
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from typing import Any


COMFY_TOOL = {
    "type": "function",
    "function": {
        "name": "comfy_img2img",
        "description": (
            "用本机 ComfyUI 对一张已有图片做图生图，返回生成图片的绝对路径。"
            "需要本机 ComfyUI 正在运行；会占用 GPU。"
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
                    "description": "采样步数 1-150，默认 20",
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
        return float(os.environ.get("PA_COMFY_TIMEOUT", "180"))

    except ValueError:
        return 180.0


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


def build_workflow(
    *,
    image_name: str,
    prompt: str,
    negative: str = "blurry, low quality",
    steps: int = 20,
    cfg: float = 7.0,
    denoise: float = 0.6,
    seed: int = 0,
) -> dict:
    if not image_name:
        raise ValueError("image_path 不能为空")

    if not prompt:
        raise ValueError("prompt 不能为空")

    if not 1 <= int(steps) <= 150:
        raise ValueError("steps 必须在 1..150")

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

    workflow = build_workflow(
        image_name=str(args.get("image_path") or "").strip(),
        prompt=str(args.get("prompt") or "").strip(),
        negative=str(args.get("negative") or "blurry, low quality"),
        steps=int(args.get("steps") or 20),
        cfg=float(args.get("cfg") or 7.0),
        denoise=float(args.get("denoise") or 0.6),
        seed=int(args.get("seed") or 0),
    )

    if cancel_token is not None:
        cancel_token.check()

    queued = _request("POST", "/prompt", {"prompt": workflow, "client_id": "personal-agent"})
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


TOOL_DEFINITIONS = (
    (COMFY_TOOL, "ask", comfy_img2img),
)
