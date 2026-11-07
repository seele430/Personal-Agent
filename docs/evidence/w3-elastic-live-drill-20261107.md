# W3 弹性闭环：真机演练记录（2026-11-07）

> 目的：在**真实的 `platform.db`**（不是隔离目录）上跑一次扩缩容闭环，
> 验证控制器、节点注册/摘除、事件落库在既有数据上也能成立。
> 原则：先备份、跑完还原、以哈希与行数双重校验。

## 1. 环境与备份

| 项 | 值 |
|---|---|
| 时间 | 2026-11-07 12:19:31（+08:00） |
| 数据库 | `C:\Users\123\Personal Agent\platform.db` |
| 备份 | `C:\Users\123\Documents\deepseek-harness\default-workspace\platform.db.bak-20261107-121931` |
| 原始 SHA256 | `8B92FE3A36CAC89C99813A77C51A7A832A31A233997E115CBD4FD4E2B1355495` |

演练前状态：

```
nodes=2  sandboxes=1  execs=2  scale_events=(无该表)
```

## 2. 演练前发现（值得单独记一笔）

两个节点在 `agent_nodes` 里都存在，但**租约都已过期**（应用没在跑，心跳停了）：

```
CAPACITY_BEFORE {'nodes_total': 2, 'nodes_accepting': 0, 'nodes_expired': 2}
```

这正是 R7 口径的价值：**实际容量按 `accepting` 算，不是按行数算**。
如果页面显示“2 个节点所以容量 2”，这次演练会直接给出错误结论。

## 3. 负载与决策

用真实的 `ExecQueue`（`max_active=0`，保证全部排队）注入 20 条待执行：

```
LOAD {'active': 0, 'queued': 20, 'max_active': 0, ...}
```

策略：`capacity_per_node=8 / headroom=0 / scale_up_after=1 / cooldown=0 / max_nodes=3`。

扩容（每 tick 真实注册一个节点）：

```
TICK_UP scale_up load_above_capacity 0 -> 1 applied True
TICK_UP scale_up load_above_capacity 1 -> 2 applied True
TICK_UP scale_up load_above_capacity 2 -> 3 applied True
AFTER_UP {'nodes': 5, 'accepting': 5, 'scale_events': 3}
```

清空队列后缩容（真实 drain + remove）：

```
TICK_DOWN scale_down load_below_capacity 3 -> 2 applied True
TICK_DOWN scale_down load_below_capacity 2 -> 1 applied True
TICK_DOWN none      steady               1 -> 1 applied False
```

## 4. 落库事件（5 条，全部 `source=live`）

```
04:19:43.557  scale_up    0 -> 1  load_above_capacity
04:19:43.574  scale_up    1 -> 2  load_above_capacity
04:19:43.593  scale_up    2 -> 3  load_above_capacity
04:19:43.610  scale_down  3 -> 2  load_below_capacity
04:19:43.638  scale_down  2 -> 1  load_below_capacity
```

事件时间均为墙钟（R3 修复后的行为），`before/after` 连续、无跳跃。

## 5. 还原与校验

| 校验 | 结果 |
|---|---|
| 还原后 SHA256 | `8B92FE3A…B1355495`，与备份一致 |
| 行数还原 | `nodes=2 sandboxes=1 execs=2 scale_events=(无该表)` |
| 残留 | 无演练节点、无演练事件、无沙箱目录（演练未创建沙箱） |

说明：演练过程中因输出过滤误将脚本再次执行了一次，已**再次还原**并以上表为准；
备份文件保留在工作区，未纳入仓库。

## 6. 结论

1. 控制器可以作用于**既有真实数据**：过期节点被正确排除，容量从 0 起算；
2. 扩缩容是真实写操作：节点被注册、被 drain、被 remove，不是模拟；
3. 事件只在真实容量变化时落库，5 条与动作一一对应；
4. 还原可验证、无残留 —— 这类演练可以重复做，风险可控。

复现方式：`scripts/demo_elastic_loop.py`（隔离目录，自带断言与安全闸门）；
真机演练需自行备份后按本文步骤执行。
