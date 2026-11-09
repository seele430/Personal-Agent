"""Provider 接力：可重试错误才换下一个；fatal 立即失败。

范围（写死）：只处理文本产物；不接力 QQ 消息、外部 API 写入等不可撤销副作用。

v1.4k：执行期间心跳续租；产物在 staging 生成，提交走事务内 fencing 复核。
"""

from __future__ import annotations

import hashlib
import json
import threading

from pathlib import Path

from src.collab.providers.openai_compat import ProviderError
from src.collab.run_store import DEFAULT_LEASE_SECONDS, RunStore


RETRYABLE = "retryable"
FATAL = "fatal"
UNKNOWN = "unknown"

STEP_ID = "compose"

RETRYABLE_HINTS = ("timeout", "timed out", "429", "500", "502", "503", "504", "连接")
FATAL_HINTS = ("401", "403", "400", "unauthorized", "forbidden", "invalid api key", "未配置")


def classify_error(error: Exception) -> str:
    status = getattr(error, "status", None)

    if status in (401, 403, 400):
        return FATAL

    if status in (429, 500, 502, 503, 504):
        return RETRYABLE

    message = str(error).lower()

    if any(hint in message for hint in FATAL_HINTS):
        return FATAL

    if any(hint in message for hint in RETRYABLE_HINTS):
        return RETRYABLE

    return UNKNOWN


class Heartbeat:
    """执行期间按租约 1/3 续租；心跳失败即标记 stale。"""

    def __init__(self, store: RunStore, run_id: str, token: int, lease_seconds: float):
        self.store = store
        self.run_id = run_id
        self.token = token
        self.lease_seconds = lease_seconds
        self.interval = max(lease_seconds / 3, 0.05)
        self.stale = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> "Heartbeat":
        self._thread.start()

        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            if not self.store.heartbeat(self.run_id, self.token, lease_seconds=self.lease_seconds):
                self.stale = True

                return

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


class RelayRunner:
    def __init__(
        self,
        *,
        store: RunStore,
        providers: dict,
        artifact_root: str | Path,
        owner: str = "cli",
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ):
        self.store = store
        self.providers = providers
        self.artifact_root = Path(artifact_root)
        self.owner = owner
        self.lease_seconds = lease_seconds

    # ---------- 入口 ----------

    def start(self, run_id: str) -> dict:
        claimed = self.store.claim(run_id, self.owner, lease_seconds=self.lease_seconds)

        if claimed is None:
            return {"run_id": run_id, "state": "not_claimed"}

        return self._execute(claimed)

    def resume(self, run_id: str) -> dict:
        run = self.store.resume(run_id)
        claimed = self.store.claim(run["run_id"], self.owner, lease_seconds=self.lease_seconds)

        if claimed is None:
            return {"run_id": run_id, "state": "not_claimed"}

        return self._execute(claimed)

    def continue_run(self, run_id: str, message: str) -> dict:
        run = self.store.continue_run(run_id, message)
        claimed = self.store.claim(run["run_id"], self.owner, lease_seconds=self.lease_seconds)

        if claimed is None:
            return {"run_id": run_id, "state": "not_claimed"}

        return self._execute(claimed)

    # ---------- 执行 ----------

    def _execute(self, run: dict) -> dict:
        run_id = run["run_id"]
        revision = int(run["revision"])
        token = int(run["fencing_token"])
        chain = run["chain"]
        index = int(run["next_provider_index"])
        attempt_base = int(run.get("attempts") or 0)

        heartbeat = Heartbeat(self.store, run_id, token, self.lease_seconds).start()

        try:
            begin = self.store.step_begin(run_id, revision, STEP_ID, run["task"], token)

            if begin == "needs_review":
                self.store.mark_needs_review(run_id, token, "step pending review")

                return {"run_id": run_id, "state": "needs_review", "revision": revision}

            if begin == "skip":
                self.store.finish(run_id, token, "succeeded", provider=run.get("provider"))

                return {"run_id": run_id, "state": "succeeded", "revision": revision, "reused": True}

            prompt = self._prompt(run, revision)
            staging = self.artifact_root / ".staging" / f"{run_id}-{token}"
            staging.mkdir(parents=True, exist_ok=True)
            target_rel = f"{run_id}/r{revision}/result.md"

            for position in range(index, len(chain)):
                name = chain[position]
                provider = self.providers.get(name)
                attempt_no = attempt_base + (position - index) + 1

                if provider is None:
                    self.store.attempt_finish(
                        self.store.attempt_start(run_id, revision, attempt_no, name, token),
                        "failed",
                        error_class=FATAL,
                        error="provider 未配置",
                    )
                    self.store.step_failed(run_id, revision, STEP_ID, token)
                    self.store.finish(run_id, token, "failed", provider=name, error_class=FATAL, error="provider 未配置")

                    return {"run_id": run_id, "state": "failed", "error_class": FATAL, "revision": revision}

                attempt_id = self.store.attempt_start(
                    run_id,
                    revision,
                    attempt_no,
                    name,
                    token,
                    injected=bool(getattr(provider, "injected", False)),
                )

                try:
                    staged_path, data = self._submit(provider, prompt, run_id, revision, staging)
                except Exception as error:
                    error_class = classify_error(error)
                    self.store.attempt_finish(attempt_id, "failed", error_class=error_class, error=str(error)[:300])
                    self.store.set_next_provider(run_id, token, position + 1, name)

                    if error_class != RETRYABLE:
                        self.store.step_failed(run_id, revision, STEP_ID, token)
                        self.store.finish(run_id, token, "failed", provider=name, error_class=error_class, error=str(error)[:300])

                        return {"run_id": run_id, "state": "failed", "error_class": error_class, "revision": revision}

                    continue

                manifest = {
                    "path": target_rel,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                }
                committed = self.store.commit_step_artifact(
                    run_id,
                    revision,
                    STEP_ID,
                    token,
                    staging_path=str(staged_path),
                    target_path=str(self.artifact_root / target_rel),
                    manifest=manifest,
                )

                if not committed:
                    self.store.attempt_finish(attempt_id, "failed", error_class=UNKNOWN, error="stale_worker")

                    return {"run_id": run_id, "state": "stale_worker", "revision": revision}

                self.store.attempt_finish(attempt_id, "succeeded")
                self.store.set_next_provider(run_id, token, position + 1, name)
                self.store.finish(run_id, token, "succeeded", provider=name)

                return {
                    "run_id": run_id,
                    "state": "succeeded",
                    "provider": name,
                    "revision": revision,
                    "manifest": manifest,
                }

            self.store.step_failed(run_id, revision, STEP_ID, token)
            self.store.finish(run_id, token, "failed", error_class=RETRYABLE, error="chain exhausted")

            return {"run_id": run_id, "state": "failed", "error_class": RETRYABLE, "revision": revision}

        finally:
            heartbeat.stop()

            if heartbeat.stale:
                self.store.audit(run_id, revision, "heartbeat_stale")

    @staticmethod
    def _prompt(run: dict, revision: int) -> str:
        messages = json.loads(run.get("messages_json") or "[]")
        lines = [run["task"]]

        for message in messages:
            lines.append(message.get("content", ""))

        if revision > 1 and len(lines) == 1:
            lines.append("请基于已有结果继续补充。")

        return "\n\n".join(line for line in lines if line.strip())

    @staticmethod
    def _submit(provider, prompt: str, run_id: str, revision: int, staging: Path) -> tuple[Path, bytes]:
        result = provider.submit(
            prompt,
            artifact_name=f"relay-{run_id}-r{revision}.md",
            out_dir=str(staging),
        )
        path = Path(result.get("artifact") or "")

        if not path.exists():
            raise ProviderError("Provider 未产出文件")

        data = path.read_bytes()

        if not data.strip():
            raise ProviderError("Provider 产物为空")

        return path, data
