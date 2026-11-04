# 复盘：默认节点租约过期，导致建沙箱永久失败

- **日期**：2026-11-04
- **发现方式**：D27 压测（`scripts/bench_platform.py`）第二轮启动时报错
- **严重度**：高 —— 任何闲置超过 30 秒的部署都无法再创建沙箱
- **状态**：已修复 + 有回归测试

## 现象

压测脚本第二次运行（复用上一轮留下的数据目录）时，建沙箱直接失败：

```text
建沙箱失败：429 {'error': {'code': 'NO_READY_NODE', 'message': '没有可接收新沙箱的节点（cordon / drain / 租约过期）'}}
```

服务是**刚启动**的，节点却是「过期」的。

## 时间线

| 时间 | 事件 |
|---|---|
| 12:01 | 压测第二轮启动，建沙箱 429 `NO_READY_NODE` |
| 12:02 | `GET /v1/nodes` → `node_local` 的 `state=not_ready`、`expired=true` |
| 12:02 | 读 `ensure_default_node()`：存在就原样返回，**从不续租** |
| 12:03 | 确认租约：默认心跳间隔 10s × 3 = 30s；数据目录是上一轮 45 分钟前留下的 |
| 12:04 | 修复：`ensure_default_node()` 对「存在且未 removed」的节点顺手续租（代际不变） |
| 12:05 | 补 3 条测试，含「把 last_heartbeat 拨回 5 分钟前再 create」的复现路径 |
| 12:07 | 全量回归 **84 文件 / 754 用例 PASS** |

## 根因

`node_local` 是平台自己的节点，但注册只在「第一次创建沙箱」时发生一次。
D18 给节点加了租约（3 个心跳周期 = 30 秒），却没有任何人给它续约：

- 平台没有心跳线程；
- `ensure_default_node()` 只 `get`，不 `heartbeat`。

因此：**闲置 30 秒 → 租约过期 → `accepting=false` → 之后所有 create 都 429**。

## 为什么测试没抓到

每个用例都用全新的临时数据库，节点总是在「刚注册」的租约窗口内。
没有任何用例覆盖「过了租约再创建」—— 这正是「按天完成任务」容易漏掉的缝隙。

## 修复

```python
# src/platform/node_registry.py
def ensure_default_node(self, *, now=None):
    try:
        node = self.get(DEFAULT_NODE_ID, now=now)
    except NodeNotFound:
        return self.register(node_id=DEFAULT_NODE_ID, now=now)

    if node["removed"]:
        return node  # 运维的显式决定，不复活

    return self.heartbeat(DEFAULT_NODE_ID, generation=node["generation"], now=now)
```

语义：**平台进程活着 = 它自己的节点活着**；removed 的节点仍然不复活。

## 整改

1. 代码：`ensure_default_node` 续租（代际不变）。
2. 测试：`tests/integration/test_default_node_renewal.py` 三条用例。
3. 流程：压测脚本保留在 `scripts/bench_platform.py` —— 这次是它把问题顶出来的，
   以后每次大改都跑一轮。

## 教训

- **「注册过」不等于「还活着」**：有租约的组件，必须有人定期续约，且续约方要明确。
- **全新数据库的测试会掩盖时间相关 bug**：至少留一条「推进时钟」的用例。
- 压测不只是测性能，它会把「闲置/重启/复用状态」这些真实路径走一遍。
