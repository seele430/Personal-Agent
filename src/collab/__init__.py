"""跨 Agent 协作（2026-11-13 收敛）。

在用模块：orchestrator（/task 编排）、platform_runner（平台沙箱执行）、
dsh_runner（DeepSeek headless）、relay / run_store / artifacts / providers
（接力任务链路，QQ 与工具层在用）。

已归档（见 legacy/README.md）：
- legacy/qwen-controlled-runner/：qwen 受控 Runner 旧栈（脚本专用，未接线）
- legacy/collab-v1.4/：capability / guard / verify / registry / handoff /
  review_bus（超前建设的协作接口层，从未接线）
"""
