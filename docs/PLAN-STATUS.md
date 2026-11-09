# 计划完成度（对照 `docs/PLAN-40D.md`）

> 更新：2026-11-04。口径：按天 + 按验收；「门」单独判定。

## 总览

| 口径 | 数字 |
|---|---|
| 与计划内容一致完成 | 25 / 40（D1–D24 + D30 主体） |
| 换题交付（做了别的事） | D25、D26 → 已补齐为 D25'/D26' |
| 部分完成 | D28（命令 ✅ / 视频 ❌）、D40（文档 ✅ / 视频 ❌） |
| 已砍（附理由） | D31–D36 |
| 门 | G1 ✅ · G2 ✅ · G3 ❌（缺视频）· G4 ✗（C++ 砍） · G5 见下 |

## D31–D36 为什么砍

本机没有 Docker，也没有任何 C++ 工具链（`docker` / `cl` / `g++` / `clang++` / `cmake` 全缺）。
计划第 5 节的「砍单顺序」本来就把**容器 tier** 和 **C++ SDK** 列在可砍项；C++ runner（D34）
依赖编译器，同样无法在本机验证。**没验证过的东西不写进交付。**

砍掉后仍成立的能力：进程树治理（Job Object，本来就是 D34 的替代方案）、隔离（工作区 +
环境清洗，无网络隔离 —— 见威胁模型）、Python SDK 契约（D10/D36 的 Python 半边）。

## 逐项

| 天 | 计划 | 状态 | 证据 |
|---|---|---|---|
| D11–D14 | 沙箱执行 / 6 工具 / 审批门 / 轨迹 CLI | ✅ | `d11`–`d14` |
| D15 | 幂等 create | ✅＋（附赠 `/v1` HTTP） | `d15-*` |
| D16 | reaper + 410 | ✅ | `d16-*` |
| D17–D19 | 节点 / 租约 / cordon·drain | ✅ | `d17`–`d19` |
| D20 | 状态机 | ✅ | `d20-sandbox-state.txt` |
| D21 | 配额 | ✅ | `d21-quota.txt` |
| D22 | 启动对账 | ✅ | `d22-reconcile.json` |
| D23 | **G2** 全链路 | ✅ | `d23-g2-chain.json` |
| D24 | traceparent | ✅ | `d24-trace.txt` |
| D25 | `/metrics` | ✅（补） | `d25-metrics-logs.txt` |
| D26 | JSONL + trace_id | ✅（补） | 同上 |
| D27 | 压测脚本 | ✅ | `scripts/bench_platform.py` |
| D28 | **G3**：3 命令 + 60s 视频 | ⚠️ 命令冻结；**视频延后到首次真实使用之后** | `docs/G3.md` |
| D29 | 饱和曲线 | ✅ | `docs/bench/SATURATION.md` |
| D30 | 背压 + 配置开关 + SLO 草案 | ✅ | `d30-*`、`docs/SLO.md` |
| D31–D33 | 容器 tier / 镜像 / 隔离 | ✂️ 砍（无 Docker） | — |
| D34–D35 | C++ runner / SDK | ✂️ 砍（无编译器） | — |
| D36 | **G4** 双 SDK 契约 | ✂️ 随 C++ 砍 | 仅 Python 契约（D10）保留 |
| D37 | 告警闭环 | ✅ | `test_platform_alerts.py`、`alert.fired` 审计 |
| D38 | 故障注入 | ✅ | `docs/evidence/d38-fault-injection.json` |
| D39 | runbook | ✅ | `docs/RUNBOOK.md` |
| D40 | 复盘 / 架构 / 简历 / 交付 | ✅ 文档；❌ 视频 | `postmortem.md`、`architecture.md`、`resume.md` |

## 五个门

| 门 | 判定 | 说明 |
|---|---|---|
| G1 | ✅ | D9 已过 |
| G2 | ✅ | D23，一句话 3 工具 + 轨迹 |
| G3 | ❌ | 3 条命令已冻结并可一键跑（19s / 3 PASS），**缺 60 秒录屏** |
| G4 | ✂️ | 依赖 C++ SDK；本机无编译器，Python 契约测试仍在 |
| G5 | ⚠️ | DoD：quickstart ✅ / 视频 ❌ / SLO ✅ / bench ✅ / runbook ✅ / postmortem ✅ / architecture ✅ / 简历 ✅ |

## 还剩什么

1. **首次真实使用**（先于录屏）：按 README/`scripts/run-dev.ps1` 把应用跑起来，自己用一遍，
   记录硌手的地方 —— 演示视频应该在「用顺手之后」录，而不是在「脚本能跑」时录。
2. **G3 录屏**（首次使用、修完问题之后再录）：`docs/G3.md` 里的 3 条命令已冻结，
   `scripts/demo_g3.py` 实测 3/3 PASS（约 25 秒）。
3. （可选）若换到有 Docker / C++ 的机器，再补 D31–D36。
