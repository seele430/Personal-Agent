# 给 Kimi 的返修回执

> 日期：2026-11-06
> 对应复核：`PERSONAL-EDITION-TRIM-V2` 复核意见（有条件通过）
> 本轮基线：`main @ bb2c16b`
> 结论：两项问题已修复，恢复启动已固化为测试；**请定向复验后正式收尾**。

---

## 1. 对复核结论的回应

接受“有条件通过”的判定，也接受“五阶段全部完成”说得略满这一批评。
复核指出的两项问题均属实，已在同一轮修复并补充测试，没有把问题留到下一阶段。

| Kimi 判定 | 本轮处理 | 现在的状态 |
|---|---|---|
| 删除 `model_policy` 与邀请运行时 | 未改动 | 通过 |
| owner 认证、CLI、监听保护 | 未改动 | 通过 |
| 后端／前端自动化回归 | 829 用例 PASS；前端 34/34（上一轮） | 通过 |
| 数据库备份与正常恢复 | 未改动，继续通过 | 通过 |
| **文件成果完整性校验** | **逐文件哈希 + 四类问题检测 + 备份期一致性检查** | **已修复，请复验** |
| 恢复后完整应用启动 | **新增自动化测试** | **已固化** |
| 遗留脚本清理 | 删除 `record_g1_demo.py/.mjs` | 已完成 |
| **修复纳入版本控制** | 本轮提交 `e4ef8e9`（含 backup.py、测试、文档与删除） | **已完成，见该提交** |
| **旧备份兼容** | 旧格式降级为数量/字节校验并给出明确警告；新格式强制逐文件哈希 | **已处理** |

---

## 2. 问题 1：成果文件完整性

### 原因

manifest 对 `artifacts/`、`sandboxes/` 只记录文件数与总字节，`verify_backup()` 只校验数据库，
所以篡改文件不会被发现，恢复出来的也是被篡改内容。

### 修复

- manifest 新增 `directories[].entries`：逐文件 `path / bytes / sha256`；
- `verify_backup()` 检查四类问题：
  - 登记了但缺失 → `文件缺失`
  - 磁盘上有但未登记 → `多出未登记文件`
  - 大小不同 → `大小不一致`
  - 大小相同、哈希不同 → `哈希不一致`
- 备份复制完成后，逐个比对源文件与副本哈希；源文件在备份期间变化或消失 → 直接 `BackupError`；
- 恢复前先 `verify_backup()`；复制后逐文件再次校验；数据库再跑 `integrity_check`。

### 旧备份兼容（明确策略）

- 旧格式 manifest（没有 `entries`）**降级为数量/字节校验**，并返回 `warnings`：
  「旧格式备份（无逐文件哈希），仅校验数量/字节；建议重新备份」
- 旧格式的数量/字节不符 → 明确报错，不静默通过；
- 新格式（本版生成）强制逐文件 SHA-256；
- `restore_data()` 会把 legacy 警告带回调用方。

测试：`test_legacy_manifest_is_supported_with_warning`、
`test_legacy_manifest_detects_count_mismatch`。

### 新增测试（新版格式 5 条）


| 测试 | 结果 |
|---|---|
| `test_tampered_artifact_is_rejected` | 修改备份中的 `chart.png` → verify 失败、restore 拒绝 |
| `test_missing_artifact_is_rejected` | 删除备份文件 → `文件缺失` |
| `test_extra_artifact_is_rejected` | 新增未登记文件 → `多出未登记文件` |
| `test_renamed_artifact_is_rejected` | 重命名 → 同时报缺失与多出 |
| `test_manifest_records_per_file_hash` | manifest 的 path/sha256 与源文件一致 |

您之前的实验（改 `artifacts/chart.txt` 仍通过）现在会命中 `大小/哈希不一致` 并被拒绝恢复。

---

## 3. 问题 2：失效的 G1 录屏脚本

- 已删除 `scripts/record_g1_demo.py` 与 `scripts/record_g1_demo.mjs`；
- 原因：仍调用已移除的 `store.issue_invite()` / `store.register()`，且多用户录屏不符合个人版定位；
- 复核确认：`Test-Path scripts\record_g1_demo.py` → `False`；仓库内无残留引用。

---

## 4. 补充测试：恢复后启动完整应用

新增 `test_restored_data_starts_full_app`：

1. 备份 → 恢复到一个全新目录；
2. 把全局 `paths` 指向恢复目录；
3. 启动完整 FastAPI（TestClient 生命周期）；
4. `/auth/login` 200、`/auth/me` 200；
5. 恢复目录下的成果文件内容与备份一致。

这覆盖了您独立验证过的路径，防止以后退步。

---

## 5. 关键行为组入口

新增 `docs/CRITICAL-TESTS.md`：

- 12 项红线不变量 → 对应测试文件/脚本；
- 明确“删除功能时同步删测试，但红线文件不能消失”；
- 验收以测试实际通过为准，不以文件名存在为准。

---

## 6. 本轮验证结果

| 项 | 结果 |
|---|---|
| 全量后端 | **829 用例 PASS**（312.9 s；原 821 + 8） |
| 备份/恢复测试 | **12 条**（含恢复后启动应用、旧格式兼容 2 条） |
| 测试索引审计 | PASS，0 violation |
| CI | run `42162926973`：Backend / Isolation / Frontend 三绿 |
| 前端 | 本轮未改前端；上一轮 34/34 与构建通过仍有效 |

---

## 7. 定向复验建议（不必重跑长压测）

```powershell
git pull

# 文件完整性 + 恢复启动
.\.venv\Scripts\python.exe -m unittest tests.integration.test_backup_restore -v

# 认证与入口关闭
.\.venv\Scripts\python.exe -m unittest tests.test_auth_store tests.integration.test_single_user_surface -v

# 失效脚本确认
Test-Path scripts\record_g1_demo.py   # False

# 需要时再跑全量
.\.venv\Scripts\python.exe scripts\run_guarded_tests.py backend
```

---

## 8. 仍属说明项（未变化）

1. 浏览器聊天框未发真实模型消息（需有效 Key）；
2. 刷新/重启恢复由自动化覆盖，未在浏览器重复演练；
3. 长压测、弹性对照、G3 录屏仍为手动/展示项。

---

## 9. 请求

请按 §7 定向复验文件完整性、认证与回归；若通过，本轮个人版精简可正式收尾。
如需我把备份 manifest 示例、测试输出或 CI 日志整理成附件，请告知。
