"""Qwen Code 外部 Agent Provider：把任务交给本机 qwen CLI，回收产物。

与 `openai_compat` 的本质区别：

- `openai_compat` 是**一次补全**，循环由我们编排；
- 本模块是**独立 agent 进程**，模型自己决定调哪些工具、写哪些文件。

安全约束（写死，不靠调用方自觉）：

- 每次任务一个**空临时工作区**，qwen 的 cwd 只在那里；
- **超时必杀整棵进程树**（Windows 用 `taskkill /T /F`）；
- 环境变量白名单，不把 PA 的其它密钥带进去；
- 产物上限：单文件 ≤1 MB、最多 10 个；
- 日志不打印 prompt 全文（只记长度与哈希前缀）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid

from pathlib import Path

from src.collab.providers.openai_compat import ProviderError


QWEN_DEFAULT_PATHS = (
    r"C:\Users\123\AppData\Roaming\npm\qwen.cmd",
    r"C:\Users\123\AppData\Roaming\npm\qwen",
)

MAX_FILES = 10
MAX_FILE_BYTES = 1_000_000
DEFAULT_TIMEOUT = 180.0

ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SystemRoot",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOME",
    "APPDATA",
    "LOCALAPPDATA",
    "COMSPEC",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "MODEL",
)


class QwenAgentProvider:
    name = "qwen-agent"

    def __init__(
        self,
        *,
        qwen_path: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_files: int = MAX_FILES,
        max_file_bytes: int = MAX_FILE_BYTES,
        popen=None,
    ):
        self.qwen_path = qwen_path or self._locate()
        self.timeout = timeout
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self._popen = popen or subprocess.Popen

    @staticmethod
    def _locate() -> str:
        found = shutil.which("qwen")

        if found:
            return found

        for candidate in QWEN_DEFAULT_PATHS:
            if Path(candidate).is_file():
                return candidate

        return ""

    @property
    def configured(self) -> bool:
        return bool(self.qwen_path)

    def submit(self, prompt: str, *, artifact_name: str, out_dir: str | Path) -> dict:
        text = (prompt or "").strip()

        if not text:
            raise ProviderError("prompt 不能为空")

        if not self.configured:
            raise ProviderError(
                "找不到 qwen 命令：请先 npm i -g @qwen-code/qwen-code"
            )

        root = Path(out_dir)
        root.mkdir(parents=True, exist_ok=True)
        workspace = root / f"qwen-workspace-{uuid.uuid4().hex[:8]}"
        workspace.mkdir(parents=True, exist_ok=True)

        # 实测：给 qwen 追加任何"产物要求"后缀，都会让它只回复文本、不动工具；
        # 所以原样传 prompt，由调用方在 prompt 里写清文件名。
        instruction = text

        started = time.perf_counter()
        code, output = self._run(instruction, workspace)
        elapsed = round(time.perf_counter() - started, 2)

        target = workspace / artifact_name

        if not target.is_file():
            raise ProviderError(
                f"Qwen Agent 没有产出 {artifact_name}（exit={code}）。"
                f"prompt 里要明确要求创建这个文件。Agent 输出：{output[-160:]}"
            )

        artifacts = self._collect(workspace)
        artifact_info = [
            {"name": item.name, "bytes": item.stat().st_size} for item in artifacts
        ]
        size = target.stat().st_size
        digest = hashlib.sha256(target.read_bytes()).hexdigest()

        # 把产物挪到 out_dir 根下，工作区保持干净
        final = root / artifact_name
        shutil.move(str(target), str(final))

        return {
            "task_id": f"qwen-{uuid.uuid4().hex[:12]}",
            "provider": self.name,
            "model": "qwen-agent",
            "artifact": str(final),
            "artifacts": artifact_info,
            "duration_s": elapsed,
            "usage": {"prompt": 0, "completion": 0, "total": 0},
            "prompt_chars": len(text),
            "prompt_sha256_8": hashlib.sha256(text.encode("utf-8")).hexdigest()[:8],
            "artifact_bytes": size,
            "sha256": digest,
        }

    def _run(self, instruction: str, workspace: Path) -> tuple[int, str]:
        command = [
            self.qwen_path,
            instruction,
            "--approval-mode",
            "yolo",
            "--auth-type",
            "openai",
        ]
        env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
        env.setdefault("QWEN_HOME", str(workspace / ".qwen-home"))

        process = self._popen(
            command,
            cwd=str(workspace),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        try:
            output, _ = process.communicate(timeout=self.timeout)

        except subprocess.TimeoutExpired:
            self._kill_tree(process)
            process.kill()
            process.communicate()
            raise ProviderError(f"Qwen Code 超时（{self.timeout}s 未完成）") from None

        return int(process.returncode or 0), (output or "")

    @staticmethod
    def _kill_tree(process) -> None:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def _collect(self, workspace: Path) -> list[Path]:
        files = []

        for path in sorted(workspace.rglob("*")):
            if not path.is_file():
                continue

            if ".qwen-home" in path.parts:
                continue

            if path.stat().st_size > self.max_file_bytes:
                raise ProviderError(f"产物过大：{path.name} 超过 {self.max_file_bytes} 字节")

            files.append(path)

        if len(files) > self.max_files:
            raise ProviderError(f"产物过多：{len(files)} 个，超过上限 {self.max_files}")

        return files


def describe() -> str:
    return json.dumps(
        {"name": QwenAgentProvider.name, "timeout": DEFAULT_TIMEOUT, "max_files": MAX_FILES},
        ensure_ascii=False,
    )
