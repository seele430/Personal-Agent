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
| 5 | 空文件、>20 MiB、ZIP 条目/展开超预算 | `test_empty_source`、`test_input_size_budget`、`test_zip_entry_budget`、`test_uncompressed_budget`、`test_single_entry_budget` |
| 6 | 损坏 ZIP、加密、路径穿越 → 415/422 | `test_invalid_zip`、`test_malformed_relationship_xml`、`test_encrypted_zip_rejected`、`test_zip_traversal_rejected` |
| 7 | 宏、外链、OLE/ActiveX → 415 | `test_macro_rejected`、`test_external_*` 6 条、`test_ole_object_rejected` |
| 8 | 成功/失败源哈希不变 | `test_source_modified`、`test_timeout_source_modified`、`test_missing_pdf_source_modified`、`test_invalid_pdf_source_modified` |
| 9 | argv 绝对 soffice、无 shell、独立 profile | `test_argv_profile_and_no_shell` |
| 10 | 退出 0 但无 PDF → 422 | `test_missing_pdf` |
| 11 | 空/非 PDF/无 EOF/加密/不可解析 | `test_empty_pdf`、`test_bad_pdf_header`、`test_eof_outside_window_rejected`、`test_encrypted_pdf_rejected` |
| 12 | 多个 PDF/额外文件 | `test_multiple_pdf`、`test_extra_non_pdf_output_rejected` |
| 13 | PDF >50 MiB | `test_pdf_size_budget`（patch 阈值，小 PDF 触发） |
| 14 | 非零退出/超时；无登记、清理 | `test_timeout_mapping`、`test_nonzero_exit_mapping`、`test_cleanup_failure_mapping`、`test_timeout_leftover_cleanup_fails_closed`、`test_convert_failed_destroy_raises` |
| 15 | 真实父子进程清理 | Tier1 既有回归 + `test_destroy_failure`、`test_destroy_leaves_directory`（见 §遗留） |
| 16 | 清理/登记失败 | `test_destroy_failure`、`test_destroy_leaves_directory`、`test_register_failure_cleans_output` |
| 17 | 成功响应字段、owner、preview/content | `test_success`（API）+ 真实 HTTP 证据 |
| 18 | 全局/owner 并发 429，锁释放 | `test_busy_returns_429`、`test_per_owner_busy_429`、`test_lock_released_after_office_error`、`test_lock_released_after_unhandled_error` |
| 19 | 异常后锁释放、下一次可执行 | 同上两条 release 测试 |
| 20 | 响应/日志不含正文/秘密/路径 | `test_error_messages_have_no_paths_or_secrets`、`test_no_logs_on_failure` |
| 21 | 远程只读 POST 拒绝、GET 不坏 | `test_remote_readonly_blocks_post`（POST 403、artifact GET 200） |
| 22 | 全量回归五段 | 全量日志 |

## 遗留 / 未单测项（如实披露）

1. **真实父子进程树探针**：Tier1 既有测试覆盖进程树；本模块额外验证 destroy 失败/残留。
2. **超大实际文件**：预算用 patch 小阈值验证分支，未生成 20–100 MiB 文件。
3. **加密 OOXML 真实样本**：用 ZIP 加密标志位构造验证；未用第三方加密文档。
4. **Office 排版视觉一致性**：只做 PDF 可解析/页数验证，不做像素比对。
