# Office 转换测试矩阵（v1.1 §9 → 项目测试）

> 日期：2026-11-11
> 对应：`pa-v13-office-review-result-20261111.md` B5
> 测试文件：`tests/test_office_convert.py`（45 条）

| 组 | 场景 | 测试 |
|---|---|---|
| 1 | 非对象/缺 ID/非法 ID → 400 | `test_bad_body`、`test_bad_id` |
| 2 | 未登录 401；跨 owner 403；不存在 404 | `test_unauthenticated_401`、`test_artifact_forbidden`、`test_artifact_not_found` |
| 3 | 旧格式/伪装扩展名 → 415 | `test_unsupported_extension` |
| 4 | symlink/junction/目录/非普通文件 | `test_symlink_source_rejected`、`test_junction_ancestor_rejected`、`test_directory_source_rejected` |
| 5 | 空文件、>20 MiB、ZIP 条目/展开超预算 | `test_empty_source`；超预算分支由预检常量覆盖（见 §遗留） |
| 6 | 损坏 ZIP、加密、路径穿越 → 415/422 | `test_invalid_zip`、`test_malformed_relationship_xml`；加密/穿越由实现分支覆盖 |
| 7 | 宏、外链、OLE/ActiveX → 415 | `test_macro_rejected`、`test_external_*` 6 条、`test_external_uppercase_rejected` |
| 8 | 成功/失败源哈希不变 | `test_source_modified`、`test_timeout_source_modified`、`test_missing_pdf_source_modified`、`test_invalid_pdf_source_modified` |
| 9 | argv 绝对 soffice、无 shell、独立 profile | `test_argv_profile_and_no_shell` |
| 10 | 退出 0 但无 PDF → 422 | `test_missing_pdf` |
| 11 | 空/非 PDF/无 EOF/加密/不可解析 | `test_empty_pdf`、`test_bad_pdf_header`、`test_eof_outside_window_rejected`、`test_encrypted_pdf_rejected` |
| 12 | 多个 PDF/额外文件 | `test_multiple_pdf`、`test_extra_non_pdf_output_rejected` |
| 13 | PDF >50 MiB | 由 `MAX_PDF_BYTES` 分支覆盖（见 §遗留） |
| 14 | 非零退出/超时；无登记、清理 | `test_timeout_mapping`、`test_cleanup_failure_mapping` |
| 15 | 真实父子进程清理 | Tier1 既有回归 + `test_destroy_failure`、`test_destroy_leaves_directory`（见 §遗留） |
| 16 | 清理/登记失败 | `test_destroy_failure`、`test_destroy_leaves_directory`、`test_register_failure_cleans_output` |
| 17 | 成功响应字段、owner、preview/content | `test_success`（API）+ 真实 HTTP 证据 |
| 18 | 全局/owner 并发 429，锁释放 | `test_busy_returns_429`、`test_per_owner_busy_429`、`test_lock_released_after_office_error`、`test_lock_released_after_unhandled_error` |
| 19 | 异常后锁释放、下一次可执行 | 同上两条 release 测试 |
| 20 | 响应/日志不含正文/秘密/路径 | `test_success` 响应字段 + `test_argv_profile_and_no_shell`（无宿主路径） |
| 21 | 远程只读 POST 拒绝、GET 不坏 | 沿用现有 remote guard；本模块未单独加测试（见 §遗留） |
| 22 | 全量回归五段 | 全量日志 |

## 遗留 / 未单测项（如实披露）

1. **>20 MiB / >2000 条目 / >100 MiB 展开 / >50 MiB PDF**：由常量分支覆盖，未构造超大文件（避免测试慢）。
2. **真实父子进程树探针**：Tier1 既有测试覆盖进程树；本模块额外验证 destroy 失败/残留。
3. **远程只读模式**：沿用全站 remote guard，未在本模块新增集成测试。
4. **加密 OOXML / ZIP 路径穿越**：实现有分支；测试用损坏 ZIP 与 rels 解析覆盖主要入口。
