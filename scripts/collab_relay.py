r"""CLI：Provider 接力（run / resume / continue）。

用法：

```powershell
python scripts\collab_relay.py run --task "..." --chain ark,deepseek
python scripts\collab_relay.py resume --run-id <ID>
python scripts\collab_relay.py resume --run-id <ID> --accept-step compose
python scripts\collab_relay.py continue --run-id <ID> --message "再补一章"
```
"""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

from src.collab import artifacts  # noqa: E402
from src.collab.providers import build_providers  # noqa: E402
from src.collab.providers.openai_compat import ProviderError  # noqa: E402
from src.collab.relay import RelayRunner  # noqa: E402
from src.collab.run_store import RunStore, StoreError  # noqa: E402


class InjectedProvider:
    """故障演练：只注入错误类型，不读、不改真实 key。"""

    def __init__(self, inner, mode: str):
        self.inner = inner
        self.name = getattr(inner, "name", "injected")
        self.mode = mode
        self.injected = True

    def submit(self, prompt: str, *, artifact_name: str, out_dir):
        error = ProviderError(f"injected {self.mode}")
        error.status = 504 if self.mode == "timeout" else 401
        error.injected = True

        raise error


def db_path() -> Path:
    return Path(os.getenv("PA_COLLAB_DB") or ROOT / "data" / "collab-runs.db")


def artifact_root() -> Path:
    return Path(os.getenv("PA_COLLAB_ARTIFACTS") or ROOT / "data" / "collab-artifacts")


def build_runner(args, chain=None) -> RelayRunner:
    store = RunStore(db_path())
    providers = build_providers(chain or args.chain.split(","))

    if getattr(args, "inject_timeout", None):
        target = args.inject_timeout
        providers[target] = InjectedProvider(providers.get(target), "timeout")

    if getattr(args, "inject_auth_error", None):
        target = args.inject_auth_error
        providers[target] = InjectedProvider(providers.get(target), "auth")

    return RelayRunner(
        store=store,
        providers=providers,
        artifact_root=artifact_root(),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="collab_relay")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run")
    run.add_argument("--task", required=True)
    run.add_argument("--chain", default="ark,deepseek")
    run.add_argument("--inject-timeout")
    run.add_argument("--inject-auth-error")

    resume = sub.add_parser("resume")
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--accept-step")
    resume.add_argument("--retry-step")
    resume.add_argument("--reset-chain", action="store_true")
    resume.add_argument("--chain")
    resume.add_argument("--inject-timeout")
    resume.add_argument("--inject-auth-error")

    cont = sub.add_parser("continue")
    cont.add_argument("--run-id", required=True)
    cont.add_argument("--message", required=True)
    cont.add_argument("--chain")
    cont.add_argument("--inject-timeout")
    cont.add_argument("--inject-auth-error")

    args = parser.parse_args(argv)
    store = RunStore(db_path())

    try:
        store.cleanup_orphans(artifact_root())

        if args.command in ("resume", "continue") and args.chain:
            store.set_chain(args.run_id, [name.strip() for name in args.chain.split(",")])

        if args.command == "run":
            run_id = store.create_run(args.task, [name.strip() for name in args.chain.split(",")])
            result = build_runner(args).start(run_id)
        elif args.command == "resume":
            current = store.get(args.run_id)

            if current is None:
                raise StoreError("run 不存在")

            revision = int(current["revision"])
            token = int(current["fencing_token"])

            if getattr(args, "reset_chain", False):
                store.reset_chain(args.run_id)
                store.audit(args.run_id, revision, "chain_reset", {})

            if args.accept_step:
                manifest = store.step_manifest(args.run_id, revision, args.accept_step)

                if manifest is None or not manifest.get("path"):
                    raise StoreError("no_text_checkpoint_supported：本轮只支持文件产物")

                if not artifacts.verify(artifact_root(), manifest):
                    raise StoreError("artifact_mismatch：产物校验失败，不能接受")

                if not store.step_accept(args.run_id, revision, args.accept_step, token, manifest):
                    raise StoreError("step_accept_failed")

                store.audit(args.run_id, revision, "step_accept", {"step_id": args.accept_step, "manifest": manifest})
                store.set_pending_after_review(args.run_id, token)
            elif args.retry_step:
                if not store.step_retry(args.run_id, revision, args.retry_step, token):
                    raise StoreError("step_retry_failed")

                store.audit(args.run_id, revision, "step_retry", {"step_id": args.retry_step})
                store.set_pending_after_review(args.run_id, token)

            stored = store.get(args.run_id) or {}
            names = set(args.chain.split(",") if args.chain else stored.get("chain") or [])
            names |= set(stored.get("chain") or [])
            result = build_runner(args, sorted(names)).resume(args.run_id)
        else:
            stored = store.get(args.run_id) or {}
            names = set(args.chain.split(",") if args.chain else stored.get("chain") or [])
            names |= set(stored.get("chain") or [])
            result = build_runner(args, sorted(names)).continue_run(args.run_id, args.message)
    except StoreError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))

    return 0 if result.get("state") == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
