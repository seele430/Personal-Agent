# 计划表：意图路由 × 弹性计算平台

> 日期：2026-11-05
> 范围：两条线并行推进，最后在「路由 → 平台执行 → 成本/观测」处汇合
> 基线：`main @ 0ac29c2`，后端 87 文件 / 771 用例 PASS
> 关联文档：`docs/ELASTIC-REPORT-20261104.md`（平台数据）、`docs/design-intent-router.md`（待写）

---

## 一、目标

| 线 | 一句话目标 | 判定 |
|---|---|---|
| **A. 意图路由** | 把「该用哪个能力」变成可度量、可解释的决策，而不是隐藏的 if-else | 评估集准确率 ≥ 90%，路由决策可见 |
| **B. 弹性计算平台** | 补齐调度/压测/时延/回收四类数据，让平台能力有数字支撑 | 均衡度 ≥ 0.90，拐点可复现，回收残留 0 |
| **汇合点** | 路由决策落到平台的 external_job / 成本闸门，形成一条完整链路 | 一条 trace 同时有 route、exec、cost |

---

## 二、总览（4 周）

```
W1  R1 路由设计  R2 决策对象  R3 路由事件        E1 多节点模拟  E2 均衡度  E3 调度时延
W2  R4 规则+分类器  R5 评估集 200 条  R6 混淆矩阵  E4 压测增强  E5 正式压测(10min×3)  E6 曲线
W3  R7 成本感知路由  R8 降级  R9 澄清阈值         E7 冷启动/回收  E8 孤儿扫描
W4  R10 路由→external_job 联调  R11 报告          E9 结果版报告    汇合验收 + 文档
```

---

## 三、A 线任务表（意图路由）

| # | 任务 | 产出 | 验收 | 依赖 | 工时 |
|---|---|---|---|---|---|
| R1 | 路由设计文档：四层（能力/模型/执行/降级）、决策对象、阈值策略 | `docs/design-intent-router.md` | 决策对象字段定死，包含 fallback 链 | — | 0.5 天 |
| R2 | `RouteDecision` + `Router` 骨架（规则层，暂不接模型） | `src/routing/` | 单测：规则命中/未命中/无匹配 | R1 | 0.5 天 |
| R3 | 路由事件落轨迹：`route_decided`（intent/confidence/reason/fallback） | `docs/trajectory-schema.md` + 测试 | 一条 run 能看到路由事件 | R2 | 0.5 天 |
| R4 | 分类器层：embedding + 逻辑回归（或小模型），带置信度 | `src/routing/classifier.py` | 离线准确率有数，p99 < 100ms | R2 | 1 天 |
| R5 | 评估集：200 条标注语料（9 类 intent，含歧义与危险样本） | `tests/data/router-eval.jsonl` | 覆盖 9 类，每类 ≥ 15 条 | — | 0.5 天 |
| R6 | 评估脚本 + 混淆矩阵 + 误路由成本 | `scripts/eval_router.py`、`docs/evidence/router-eval.json` | 准确率、各类 P/R、高危误路由率 | R4, R5 | 0.5 天 |
| R7 | 成本感知路由：按预算/时段在本地与云端 provider 间切换 | `src/routing/policy.py` | 预算耗尽后切本地，测试覆盖 | R2 | 1 天 |
| R8 | 降级链：主 provider 超时 → 备选，记录 `fallback_reason` | 同上 | 故障注入：关主 provider 能切换 | R7 | 0.5 天 |
| R9 | 歧义澄清：低置信度返回 `clarify`，不生猜 | `src/routing/` | 歧义集上误答率下降 | R4 | 0.5 天 |
| R10 | 路由 → external_job 联调：视频请求按预算选本地 CogVideoX / Kling | 集成 | 一条 trace 含 route + job + 成本 | R8 | 1 天 |
| R11 | 路由结果报告（指标表 + 混淆矩阵图） | `docs/ROUTER-REPORT.md` | 所有数字可追溯 | R6,R10 | 0.5 天 |

## 四、B 线任务表（弹性计算平台）

| # | 任务 | 产出 | 验收 | 依赖 | 工时 |
|---|---|---|---|---|---|
| E1 | 3 节点模拟（容量可配，或 3 个 edge 子进程） | `scripts/demo_scheduling.py` | 3 节点能分流；cordon 后不再接活 | 现有 `pick_node()` | 0.5 天 |
| E2 | 均衡度统计：`1 - stdev/mean`、max/min | `docs/evidence/sched-balance.json` | N=60 时均衡度 ≥ 0.90 | E1 | 0.5 天 |
| E3 | 调度时延：create → 选节点的 p50/p99 | 同上 | p99 < 10ms | E1 | 0.5 天 |
| E4 | 压测脚本增强：`--minutes`、CPU/内存采样、CSV 时间序列 | `scripts/bench_platform.py` | 单档 10 分钟不崩 | — | 0.5 天 |
| E5 | 正式压测：2/4/8/12 RPS × 10 min × 3 次 | `docs/bench/elastic-*.csv/json` | 每档 3 次，报中位数与区间 | E4 | 1 天（跑机） |
| E6 | 饱和曲线 + 预定义拐点判据 | `docs/bench/elastic-curve.png`、`ELASTIC.md` | 判据写在跑之前 | E5 | 0.5 天 |
| E7 | 冷启动 / 回收延迟分布 | `docs/evidence/lifecycle-latency.json` | p50/p95/p99 齐全 | — | 0.5 天 |
| E8 | 孤儿扫描：压测后进程/目录/DB 残留 | `scripts/check_residue.py` | 残留 = 0 | E5 | 0.5 天 |
| E9 | 平台结果版报告 | `docs/ELASTIC-REPORT-RESULTS.md` | 每个数字带证据路径 | E2,E5,E7,E8 | 1 天 |

---

## 五、里程碑

| 里程碑 | 时间 | 判定 |
|---|---|---|
| M1 路由可见 + 调度有数 | W1 末 | R1–R3、E1–E3 完成；`route_decided` 可在轨迹看到；均衡度 ≥ 0.90 |
| M2 路由可度量 + 压测有数 | W2 末 | R4–R6、E4–E6 完成；准确率 ≥ 90%；拐点可复现 |
| M3 成本/降级 + 回收干净 | W3 末 | R7–R9、E7–E8 完成；预算切换生效；残留 = 0 |
| M4 汇合 + 报告 | W4 末 | R10–R11、E9 完成；一条 trace 串起 route→job→cost |

---

## 六、要展示的指标

### A 线（路由）

| 指标 | 口径 | 目标 |
|---|---|---|
| 准确率 | 200 条标注集 | ≥ 90% |
| 高危误路由率 | 危险操作被路由到无审批路径 | ≤ 2% |
| 路由延迟 | 规则层 + 分类器 | p50 < 20ms，p99 < 100ms |
| 澄清触发率 | 低置信度占比 | 5–15% |
| 降级率 | fallback / 总请求 | < 5% |
| 成本节省 | 混合路由 vs 全强模型 | ≥ 40% |

### B 线（平台）

| 指标 | 口径 | 目标 |
|---|---|---|
| 调度均衡度 | `1 - stdev/mean` | ≥ 0.90 |
| 创建延迟 | create → ready | 给出实测 p50/p99 |
| exec 拐点 | p99 > 1s 或错误率 > 1% | 8–12 RPS 区间 |
| 回收残留 | 进程/目录/DB 行 | 0 |
| 单机并发沙箱 | 非 reclaimed 数 | ≥ 8（配额可调） |

---

## 七、汇合设计（W4 的重点）

```
用户消息
  → Router（四层决策）
      → route_decided 事件（intent/confidence/fallback）
  → 能力执行
      → 本地：sandbox exec（有界、可取消）
      → 外部：external_job（提交/轮询/工件/成本）
  → 成本闸门（按 provider 单价 × 时长）
  → 轨迹：一条 trace 串起 route → exec/job → artifact
```

验收：发一句「生成一个鲸鱼视频」，轨迹里能看到
`route_decided(video.generate, 0.87)` → `external_job(local_cogvideo)` → `artifact(...)` → `cost=0`。

---

## 八、风险与边界

| 风险 | 影响 | 应对 |
|---|---|---|
| 评估集自造、分布偏 | 准确率虚高 | 按真实对话抽样 + 公开小集交叉验证 |
| 分类器太重 | 路由延迟超过收益 | 规则优先，分类器只做兜底 |
| 路由做成 if-else 套壳 | 面试一问就露 | 必须有混淆矩阵与误路由成本 |
| 无 Docker | 多后端无法真做 | 只讲 tier1 + 外部 provider，边界写清 |
| 压测占机 | 期间不能干别的 | 放后台串行跑，记录环境 |
| 两条线互相挤占 | 都半成品 | 按周切换，W1–W2 平台优先，W3–W4 路由优先 |

---

## 九、Definition of Done

- [ ] `docs/design-intent-router.md` 定义四层决策与阈值策略
- [ ] 路由决策在轨迹中可见，含置信度与 fallback
- [ ] 200 条评估集 + 混淆矩阵 + 误路由成本
- [ ] 成本感知路由与降级链有测试
- [ ] 平台均衡度、拐点、时延、回收四类数据齐全
- [ ] 一条 trace 串起 route → exec/job → cost
- [ ] 两份结果报告（`ROUTER-REPORT.md`、`ELASTIC-REPORT-RESULTS.md`）
