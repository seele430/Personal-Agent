# 正式压测方案与判据（预定义）

> 定义时间：2026-11-05（**先写判据，后跑压测**；跑完不得修改）
> 脚本：`scripts/bench_platform.py`（开环固定 RPS、真实 uvicorn 子进程、psutil 资源采样）

## 1. 配置

| 项 | 值 |
|---|---|
| 档位 | 2 / 4 / 8 / 12 RPS |
| 每档时长 | **10 分钟**（`--minutes 10`） |
| 重复 | 关键档 **8 RPS 重复 3 次**；其余档 1 次 |
| 预热 | 每档前 30 秒不计入统计（脚本内部固定丢弃？**否**：脚本统计全段，报告里单独说明） |
| 执行容量 | `MAX_ACTIVE=4`、队列 16、排队超时 60 s |
| 限流 | 压测期间关闭（`RATE_PER_MIN=1000000`），避免 429 来自限流 |
| 资源采样 | 服务进程 CPU% / RSS，每 2 秒一次 |

> 说明：当前脚本没有「预热丢弃」实现，报告会把前 30 秒单独标注；这是已知偏差。

## 2. 拐点判据（预定义）

**拐点 = 第一个满足以下任一条件的档位：**

1. `p99 > 1000 ms`；
2. 错误率 `(非 202 且非 429) / submitted > 1%`；
3. `queue_peak == 16` 且出现 `QUEUE_FULL`（背压触发）。

三条同时看，报告里分别列出，不做事后挑选。

## 3. 要产出的表

| 档位 | offered RPS | achieved RPS | 202 | 429 | 其它错误 | p50 | p95 | p99 | queue peak | CPU avg/max | RSS avg/max |
|---|---|---|---|---|---|---|---|---|---|---|---|

外加：重复档的 3 次结果（中位数 + 区间）。

## 4. 运行命令（分段跑，避免单次过长）

```powershell
# 2 RPS × 10 分钟
.\.venv\Scripts\python.exe scripts\bench_platform.py --ladder 2 --minutes 10

# 4 RPS × 10 分钟
.\.venv\Scripts\python.exe scripts\bench_platform.py --ladder 4 --minutes 10

# 8 RPS × 10 分钟（重复 3 次）
.\.venv\Scripts\python.exe scripts\bench_platform.py --ladder 8 --minutes 10 --repeat 3

# 12 RPS × 10 分钟
.\.venv\Scripts\python.exe scripts\bench_platform.py --ladder 12 --minutes 10
```

## 5. 已知局限

- 单机、单进程、单 uvicorn worker；不代表集群。
- 任务体是 `print(1)` 的短进程，偏轻；真实工具会更重。
- 客户端与服务端同机，CPU 竞争会互相影响；报告需记录机器负载。
- 没有预热丢弃，前 30 秒偏高/偏低都会进统计。

## 6. 结果（2026-11-05，单机 12 核 / 16 GB）

每档 10 分钟，开环固定 RPS，脚本 `bench_platform.py`。

| RPS | submitted | 202 | 429 | 其它 | p50 ms | p95 ms | p99 ms | queue peak | CPU avg | RSS avg |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2 | 1200 | 1200 | 0 | 0 | 703 | 797 | 890 | 0 | 59.4% | 98.0 MB |
| 4 | 2400 | 2400 | 0 | 0 | 1016 | 1640 | 1969 | 4 | 106.2% | 100.5 MB |
| 8 | 4800 | 4800 | 0 | 0 | 3578 | 4219 | 5328 | 12 | 125.3% | 116.4 MB |

证据：

- `docs/bench/ladder-20261105-214731.csv/json`（2 RPS）
- `docs/bench/ladder-20261105-215746.csv/json`（4 RPS）
- `docs/bench/ladder-20261105-221553.csv/json`（8 RPS）

### 拐点判定（按第 2 节预定义判据）

- 2 RPS：p99 890 ms < 1000 ms，无背压 → 未触发
- **4 RPS：p99 1969 ms > 1000 ms → 触发判据 1，拐点 = 4 RPS**
- 8 RPS：p99 5328 ms，队列峰值 12（< 16），未出现 QUEUE_FULL

### 结论

- 拐点处的 p99 约 **1.97 s**，对应吞吐约 4 RPS；瓶颈是每次 exec 的 Python 启动开销（本机约 0.7–1 s）。
- 8 RPS 仍全部 202（未触发 429），但排队导致 p50 3.6 s，属于「能接但慢」。
- CPU 在 8 RPS 达 125%（多核），RSS 稳定在 98–116 MB，无内存增长迹象。

### 尚未完成

- **8 RPS 的 3 次重复**（当前只有 1 次）；
- **12 RPS 档**（预计会触发 `QUEUE_FULL`）。

> 说明：本轮在机器有其他负载的情况下运行（CPU 采样为服务进程树），数值偏保守；重复跑时应记录机器空闲状态。
