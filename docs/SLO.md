# SLO 草案 v1（D30）

> 依据：`docs/bench/SATURATION.md` 的实测曲线 + `docs/evidence/` 里的回归记录。
> 适用范围：**单机、单进程**的本地平台。多 worker / 多机不适用。
> 任何一条不达标，都算「不满足 SLO」，不解释成「环境问题」。

## 1. 可用性

| 指标 | 目标 | 当前实测 |
|---|---|---|
| `/v1` 请求非 5xx 比例 | ≥ **99.5%** / 24h | 压测四档共 260 请求，5xx = **0** |
| 平台重启后恢复服务 | ≤ **60s** | 对账 + 启动约 **10s**（D22 实测） |
| 回收后语义 | 返回 **410**，不返回 5xx | `verify_platform_v1.py` 实测 `SANDBOX_GONE` |

## 2. 延迟（exec 提交 → 终态）

| 场景 | 目标 | 当前实测 |
|---|---|---|
| ≤ 4 RPS 稳态 | p99 ≤ **600ms** | **484ms** |
| ≤ 8 RPS 稳态 | p99 ≤ **1s** | **531ms** |
| 超过容量 | 允许 429，但 **不允许 5xx**，且 p99 ≤ 5s | 12 RPS：p99 4.08s、10×`QUEUE_FULL`、0×5xx |

## 3. 背压与队列

| 指标 | 目标 | 当前实测 |
|---|---|---|
| 队列满 | 返回 429 `QUEUE_FULL`（带 `Retry-After`） | ✅ 有测试与真实服务验证 |
| 排队超时 | 到点落 `timed_out / QUEUE_TIMEOUT` | ✅ D30.2 |
| 排队时间上限 | 默认 60s，可配 | ✅ `PERSONAL_AGENT_PLATFORM_QUEUE_MAX_WAIT` |
| 无隐式无限排队 | 队列有界 | ✅ `QUEUE_SIZE` 默认 16 |

## 4. 安全与审计

| 指标 | 目标 | 当前实测 |
|---|---|---|
| 未授权访问 | 401 且留痕 | ✅ D25/D26 |
| 关键操作留痕 | create / destroy / exec / cancel / deny | ✅ `GET /v1/audit` |
| 审计可查 | 事故时不被限流挡住 | ✅ `/v1/audit` 免限流（D26.1） |

## 5. 明确不承诺的（避免误读）

1. **不是安全沙箱**：没有网络隔离、没有文件 jail（见 `docs/threat-model.md`）。
2. **不是分布式**：限流、队列、活跃表都是进程内的。
3. **不承诺多租户隔离**：`X-Tenant-Id` 由调用方自报。
4. **不承诺 exec 自动重放**：`/v1` 的 exec 没有幂等（契约 §3.2）。
5. **SLO 只在单机口径下成立**：换机器要重测（脚本在 `scripts/bench_platform.py`）。

## 6. 复核方式

```powershell
.\.venv\Scripts\python.exe scripts\bench_platform.py --ladder 2,4,8,12 --duration 10
.\.venv\Scripts\python.exe scripts\run_guarded_tests.py backend
```

两条都过 → SLO 的「延迟 / 可用性 / 背压」三项成立；证据留在 `docs/bench/` 与 `docs/evidence/`。
