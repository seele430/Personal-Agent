# 返修报告（回应 Kimi 复核）

> 日期：2026-11-06
> 复核来源：`PERSONAL-EDITION-TRIM-V2` 复核意见（有条件通过）
> 结论：两项问题已修，补充测试已固化；建议定向复验后正式收尾。

---

## 1. 问题与修复对照

| # | Kimi 发现 | 修复 | 验证 |
|---|---|---|---|
| 1 | 成果文件被篡改后备份验证仍通过（只记数量/字节） | manifest 逐文件记录 `path / bytes / sha256`；`verify_backup()` 检查缺失、额外文件、大小与哈希变化；备份期间源文件变化直接报错 | 新增 5 条测试（篡改、缺失、额外、重命名、manifest 哈希） |
| 2 | `scripts/record_g1_demo.py` 调用已删除的 `issue_invite/register`，不可运行 | 删除 `record_g1_demo.py` 与配套 `.mjs`（G1 多用户录屏不再符合个人版） | `git rm`，无残留引用 |
| 3 | 恢复测试只调 `AuthStore.login()`，未启动完整应用 | 新增 `test_restored_data_starts_full_app`：恢复目录上启动完整 FastAPI，`/auth/login`、`/auth/me`、成果文件读取全部验证 | 测试通过 |
| 4 | 关键行为组没有独立入口 | 新增 `docs/CRITICAL-TESTS.md`：12 项红线 → 对应测试文件/脚本，并写明删除时的机械检查 | 文档 |
| 5 | 旧备份 manifest 没有逐文件哈希，未明确兼容策略 | 旧格式降级为**数量/字节校验 + 明确警告**（`legacy`），不静默通过；新备份仍强制逐文件哈希 | 2 条测试（旧格式通过+提示、旧格式数量不符报错） |

---

## 2. 备份修复细节

### manifest 结构（新增 `directories[].entries`）

```json
{
  "name": "sandboxes",
  "files": 1,
  "bytes": 8,
  "entries": [
    {
      "path": "sandboxes/sbx_demo/workspace/chart.png",
      "bytes": 8,
      "sha256": "..."
    }
  ]
}
```

### 验证规则

- 登记了但磁盘上没有 → `文件缺失`
- 磁盘上有但未登记 → `多出未登记文件`
- 大小不同 → `大小不一致`
- 大小相同但哈希不同 → `哈希不一致`

### 一致性

备份复制完成后，逐个文件比对「源文件哈希」与「副本哈希」；
若源文件在备份期间被修改或删除，直接抛 `BackupError`，提示停止写入后重试。

### 恢复

恢复前先 `verify_backup()`；复制完成后再次逐文件校验哈希；数据库再跑 `integrity_check`。

---

## 3. 新增测试

| 测试 | 验证点 |
|---|---|
| `test_tampered_artifact_is_rejected` | 改备份中的 `chart.png` → verify 失败、restore 拒绝 |
| `test_missing_artifact_is_rejected` | 删备份文件 → `文件缺失` |
| `test_extra_artifact_is_rejected` | 加未登记文件 → `多出未登记文件` |
| `test_renamed_artifact_is_rejected` | 重命名 → 同时报缺失与多出 |
| `test_manifest_records_per_file_hash` | manifest 的 path/sha256 与源文件一致 |
| `test_restored_data_starts_full_app` | 恢复后启动 FastAPI：owner 登录、`/auth/me`、成果文件读取 |

备份测试合计 **12 条**（含旧格式兼容 2 条）。

---

## 4. 回归结果

| 项 | 结果 |
|---|---|
| 全量后端 | **829 用例 PASS**（312.9 s；原 821 + 8） |
| 测试索引审计 | PASS，0 violation |
| 前端 | 本轮未改前端；上一轮 34/34、构建通过 |
| CI | 待本轮推送后自动跑（见提交页） |

---

## 5. 定向复验建议（不必重跑长压测）

```powershell
# 1) 文件完整性 + 恢复启动
.\.venv\Scripts\python.exe -m unittest tests.integration.test_backup_restore -v

# 2) 认证与入口关闭
.\.venv\Scripts\python.exe -m unittest tests.test_auth_store tests.integration.test_single_user_surface -v

# 3) 全量回归（可选，约 6 分钟）
.\.venv\Scripts\python.exe scripts\run_guarded_tests.py backend

# 4) 确认失效脚本已删除
Test-Path scripts\record_g1_demo.py   # 应为 False
```

---

## 6. 仍属说明项（未变化）

- 浏览器聊天框未发真实消息（需有效模型 Key）；
- 刷新/重启恢复由自动化覆盖，未在浏览器重复演练；
- 长压测、弹性对照、G3 录屏仍为手动/展示项。
