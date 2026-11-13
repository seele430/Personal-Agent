# legacy/ — 归档区

这里是**留档不可运行**的代码：import 路径仍指向 `src.*`，移动后不再可导入，
仅作设计参考。可运行版本永远在 git 历史里（见各节「最后可用 commit」）。

## elastic-platform → 已独立成 GitHub 项目

弹性计算控制面（elastic_controller / autoscale / trends / whatif / scale_events /
alerts + 测试 + 演示脚本）已于 2026-11-13 抽离为独立仓库
**[elastic-agent-platform](https://github.com/seele430/elastic-agent-platform)**，
本目录不存副本。最后可用 commit：本仓库 `50f1ab8`（及其父链）。

## qwen-controlled-runner/（2026-11-13 归档）

Qwen 受控 Runner 旧栈：qwen_runner / qwen_pipeline / qwen_watcher /
qwen_workspace / qwen_events + 6 个单测 + qwen_task / qwen_watch /
verify_qwen_agent 脚本。

归档原因：从未接入生产（只有 scripts 引用）；`/task` 编排走的是
`src/collab/orchestrator.py` 的 call_qwen 路线，两代并存易误导。
最后可用 commit：`50f1ab8`。

## collab-v1.4/（2026-11-13 归档）

「跨 Agent 协作 v1.4」接口层：capability / guard / verify / registry /
handoff / review_bus + 4 个单测 + agent_handoff / collab_tasks /
review_watcher / verify_collab_e2e / verify_hf_agent / verify_real_provider 脚本。

归档原因：超前建设的接口层（包 docstring 自述「真实 Provider / QQ 入站不在
本轮」），从未接线；relay / run_store / artifacts / providers 是在用代码，
保留在 src/collab/。最后可用 commit：`50f1ab8`。
