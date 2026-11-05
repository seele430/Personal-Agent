# 弹性控制面结果报告

> 日期：2026-11-05
> 对应计划：`docs/PLAN-ROUTER-ELASTIC-20261105.md` W2–W3
> 证据：`docs/bench/autoscale-compare.*`、`docs/evidence/w34-residue.json`、`docs/bench/SATURATION.md`

---

## 1. 做了什么

| 能力 | 实现 | 验收 |
|---|---|---|
| 期望容量 | `src/platform/autoscale.py::desired_capacity` | 单测覆盖空闲/排队/headroom/上限 |
| 扩缩容控制器 | `Autoscaler`（扩容稳定 2 次、缩容冷却 60 s、事件） | 冷却真实生效，事件可查 |
| 节点失联停止接活 | 租约过期 → `accepting=False` → `pick_node` 跳过 | 集成测试 |
| 回收无残留 | `scripts/verify_w34_residue.py` | 目录 / DB / 进程均为 0 |
| 固定 vs 弹性对照 | `scripts/autoscale_compare.py` | 见下表 |

## 2. 固定 vs 弹性（离散时间仿真）

负载：180 个 tick，30–90 峰值 14 任务/s，90–120 降至 6/s，其余 2/s；
单节点容量 8/s；固定基线 1 节点；弹性 1–4 节点（headroom 0.1，扩容稳定 2 tick，缩容冷却 30 tick）。

| 指标 | 固定 1 节点 | 弹性 1–4 节点 | 改善 |
|---|---|---|---|
| 峰值积压 | 64 | **18** | **-71.9%** |
| 平均积压 | 26.76 | **0.49** | **-98.2%** |
| 丢弃任务 | 296 | **0** | -100% |
| 扩容事件 | 0 | 4 | — |

> 这是**仿真**，不是真实压测：用于验证控制器行为与收益方向，不能替代 D27/D29 的正式压测。

## 3. 回收与失联

| 检查 | 结果 |
|---|---|
| 沙箱销毁后残留目录 | 0 |
| 非 reclaimed 的 DB 行 | 0 |
| 匹配沙箱根路径的进程 | 0 |
| 租约过期后 `accepting` | False |
| 租约过期后 `pick_node` | None |

证据：`docs/evidence/w34-residue.json`。

## 4. 已有压测数据（早前，短时）

| 档位 | p99 | 说明 |
|---|---|---|
| 2 RPS | 344 ms | 稳定 |
| 4 RPS | 484 ms | 稳定 |
| 8 RPS | 531 ms | 无背压 |
| 12 RPS | 4.08 s | 队列峰值 16，10 次 `QUEUE_FULL` |

来源：`docs/bench/SATURATION.md`。**每档约 10 秒，不满足修订版 10 分钟 × 3 次的要求**，正式压测仍待补。

## 5. 已知局限

1. 仿真不是真实负载；节点是同机模拟，不是多机集群。
2. 单进程队列，多 worker 不共享状态（见威胁模型）。
3. 无 CPU/内存采样，密度与超卖比无法给真实数字。
4. `desired_capacity` 只看运行数与排队数，不感知真实资源。

## 6. 复现

```powershell
.\.venv\Scripts\python.exe scripts\autoscale_compare.py
.\.venv\Scripts\python.exe scripts\verify_w34_residue.py
.\.venv\Scripts\python.exe -m unittest tests.test_autoscale tests.integration.test_platform_scheduling
```
