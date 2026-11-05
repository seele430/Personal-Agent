# 意图路由结果报告

> 日期：2026-11-05
> 对应计划：`docs/PLAN-ROUTER-ELASTIC-20261105.md` W1–W2
> 证据：`docs/evidence/router-eval.json`、`tests/data/router-eval.manifest.json`

---

## 1. 做了什么

| 能力 | 实现 |
|---|---|
| 5 类业务意图 + `clarify` 决策动作 | `src/routing/router.py`（规则层） |
| 风险分级与确定性审批闸门 | `src/routing/policy.py` |
| 降级不降权限 | `src/routing/fallback.py` |
| `route_decided` 落轨迹 | `src/routing/events.py` |
| 一句话 → 沙箱 → 图表 | `scripts/demo_route_pipeline.py` |
| 评估集与评估脚本 | `scripts/build_router_eval.py`、`scripts/eval_router.py` |

**职责边界**：Router 只输出 intent / 置信度 / 风险 / 能力 / 执行约束 / 回退；不选节点、不查配额、不建沙箱。

---

## 2. 评估结果（冻结 test 集）

数据集：160 条（train 90 / dev 35 / test 35），test 集 SHA256
`ee80dd9d6995e641e9310a17ea3e5f53241b40cc32b7e2d1b24c344a2f65c545`。

| 指标 | 值 |
|---|---|
| 准确率 | **94.3%**（33/35） |
| macro-F1 | **0.963** |
| 高危漏审批 | **0** |
| clarify 触发率 | 2.9% |

| intent | P | R | F1 | 样本 |
|---|---|---|---|---|
| general.chat | 1.000 | 1.000 | 1.000 | 6 |
| data.analysis | 1.000 | 1.000 | 1.000 | 3 |
| code.execute | 1.000 | 0.875 | 0.933 | 8 |
| file.operate | 1.000 | 0.875 | 0.933 | 8 |
| platform.inspect | 1.000 | 1.000 | 1.000 | 5 |
| reminder.manage | 0.833 | 1.000 | 0.909 | 5 |

## 3. 延迟

纯规则层，本机实测（1000 次）：

| 指标 | 值 |
|---|---|
| p50 | **0.028 ms** |
| p99 | **0.36 ms** |
| 最大 | 2.08 ms |

冷加载（导入模块）未单独统计，首次调用含 Python 导入，量级在几十毫秒。

## 4. 已知局限（必须一并说明）

1. **评估集是自造的**：种子语句由我编写，存在分布偏差；160 条只够做开发集，不能据此下强统计结论。
2. **test 集被看过一次**：调规则时检查过 test 错误，94.3% 应视为**上界**，不是严格盲测。
3. **规则层靠关键词**：同义改写、长句、否定句可能失效；`clarify` 阈值是启发式。
4. **分类器层暂缓**：规则层已达 90% 目标，分类器会增加延迟与维护成本；触发条件是「规则准确率跌破 90%」或「新增 intent」。
5. **未接真实对话流**：目前通过 `demo_route_pipeline.py` 与单元测试验证；接入 `agent.chat()` 是下一步。

## 5. 复现

```powershell
.\.venv\Scripts\python.exe scripts\build_router_eval.py     # 重建评估集（会改哈希，需同步 manifest）
.\.venv\Scripts\python.exe scripts\eval_router.py --split test
.\.venv\Scripts\python.exe scripts\demo_route_pipeline.py
.\.venv\Scripts\python.exe -m unittest tests.test_intent_router tests.test_router_eval tests.test_routing_policy tests.test_routing_fallback
```
