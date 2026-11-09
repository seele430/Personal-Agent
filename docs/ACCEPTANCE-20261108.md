# Personal Agent 验收报告（v1.1 修订：吸收 2026-11-08 对账审查）

**日期**：2026-11-08（修订于同日 22:40）
**基线提交**：`ec656c6`（工作树干净，除历史未跟踪文件 `scripts/try_platform.py`）
**报告性质**：阶段验收，不是上线签发
**修订说明**：初版（`b89fd9b`）经外部对账审查后，按审查意见收窄状态、补基线证据、补过程偏差与实现漂移。

```text
PROJECT_ROOT = C:\Users\123\Personal Agent
ROADMAP_ROOT = C:\Users\123\Documents\deepseek-harness\default-workspace
EVIDENCE_ROOT = C:\Users\123\Documents\deepseek-harness\default-workspace
```

下方所有证据路径**相对 EVIDENCE_ROOT**（除标注 `PROJECT_ROOT` 者）。

---

## 0. 结论（双维度）

**成果维度**：v1.0 的分数/来源数/游戏/平台/故障指标、v1.1 短素材、v1.2 本地远程、v1.3 已完成的子集、
v1.4a 本地与模拟闭环、B1 功能链、v1.5–v1.8，均有实测证据。

**过程维度**：v1.0 的 W6 改进重跑次数超出计划预算（见 §3.1），按路线图规则整体记 **部分通过**。

因此本报告的状态表按"**成果 / 过程**"两列分别记录，不再用单一 ✅ 覆盖。

| 轨道 | 成果 | 过程 / 范围 | 综合 |
|---|---|---|---|
| v1.0 | ✅ 达标 | ⚠️ W6 重跑超预算（≥7 次 vs 2 次） | 🟡 **部分通过（成果通过 / 过程偏差）** |
| v1.1 | 短素材 ✅ / 长素材 93.3% | 无真人素材 | 🟡 **部分通过** |
| v1.2 | 本地 TLS 8/8 | 手机真机未做 | 🟡 **部分通过** |
| v1.3 | 时间线/生命周期/Diff/CLI 完成 | 模型 fallback、QQ 通知、Sleep A/B、Office 转换未做 | 🟡 **部分通过** |
| v1.4a | 本地 Registry/Guard/Verify + 模拟 Provider E2E ✅ | 无真实 Provider、无通知出站 | 🟡 **部分通过（本地骨架）** |
| v1.4b/c | — | OneBot 入站、跨 Provider 接力未做 | ⬜ **未开始** |
| B1 | A1–A6 功能证据 ✅ + pytest 原命令 ✅ | 版本路线偏离原计划，**owner 已接受**（2026-11-09） | ✅ **通过** |
| v1.5–v1.8 | ✅（本轮聚合复跑） | 未逐条重新推导每条新增判据 | ✅ **通过（有限复跑）** |

---

## 1. 验收范围

| 轨道 | 范围 |
|---|---|
| v1.0 | W0–W8（报告/贪吃蛇/平台/故障/记忆评测/发布文档） |
| v1.1 | 视频管线：探测、音轨、转写、要点采样、关键帧、截断标记、API、前端面板 |
| v1.2 | 远程只读：守卫、限速、日志轮转、自签名 TLS、owner 会话桥接 |
| v1.3 | 活动时间线、数据生命周期（DB 行 + 文件）、产物 Diff、运维 CLI |
| v1.4a | 能力三态、任务注册表、工作区租约、独立验收、CLI、模拟 Provider E2E |
| B1 | ComfyUI 工具包 A1–A6 |
| v1.5–v1.8 | 工具层接入、HTTP 端点、图生图 UI、GPU 保护四条与自检、文档一致性 |

**明确不在范围**：OneBot 入站、真实第三方 Provider、QQ/ntfy 出站、手机真机、真人视频、
LibreOffice/Office 转换、模型 fallback、Sleep A/B、GPU 压测。

---

## 2. 方法与口径

1. **四态判定**，不设"基本通过"。
2. **成果与过程分开判**：成果指标可以过，过程偏差单独记并影响综合状态。
3. **不认自述**：外部 Agent 的 `tests_passed` 只存档；`verify_collab_e2e.py` 有"哈希写错必须判 False"的反例。
4. **不降线**：所有 partial 数字原样保留，未改判据、未放宽阈值。
5. **同一基线（限 §4）**：§4 中后端、前端、E2E、混合回归与独立 mock 的数字均来自 `ec656c6`；
   长素材、B1、GPU 自检、文档一致性引用的是各自时点的历史证据，**不混作本次新运行**（见 §6 索引）。

---

## 3. 需要单独披露的三件事

### 3.1 v1.0 的 W6 过程偏差

路线图 §9.3：6 次正式运行之外 **W6 全周最多 2 次修复后重跑**，"超出预算即按事实记部分通过"。

`PROJECT_ROOT/docs/V1.0-RELEASE.md` §3.3 的逐次清单：

| 阶段 | 次数 |
|---|---|
| 原始配置 | 3 |
| 改进① | 1 |
| 改进② | 3 |
| 改进③（最终） | 3 |
| **改进后合计** | **7** |

- 无论按 7 次（§3.3 口径）还是文档里另一处写的 3 次/6 次，**都超过 2 次预算**；
- 文档内两套计次记录已在新版 §3.4 标注，未擅自改历史；
- **处理**：成果指标（3/3、11 条来源、98/98/100）保留；综合状态记 **部分通过（成果通过 / 过程偏差）**；
- 若 owner 接受该偏差，需另记"已知情接受的过程偏差"，不能追溯改预算。

### 3.2 v1.3 / v1.4 的范围收窄

| 轨道 | 之前写法 | 修正为 | 已完成 / 未完成 |
|---|---|---|---|
| v1.3 | ✅ 通过 | 🟡 部分通过 | 已：时间线（API/CLI/UI）、DB+文件生命周期、Diff（API/UI）、运维 CLI；未：模型 fallback、QQ 通知、Sleep A/B、Office 转换 |
| v1.4a | ✅ 本地闭环通过 | 🟡 部分通过（本地骨架） | 已：能力三态、注册表、租约、独立验收、CLI、模拟 HTTP E2E；未：真实 Provider、真实任务分档、通知 |
| v1.4b/c | 未单独判定 | ⬜ 未开始 | OneBot 入站、真实同任务继续、跨 Provider 接力 |

### 3.3 B1 的实现与命令漂移

| 项 | 原计划 | 实际 | 处理 |
|---|---|---|---|
| 运行时 | portable 7z（约 2 GB） | 源码路线：ComfyUI **0.3.40** + torch 2.5.1+cu121 | 记为实现变更（portable 下载停在 74.7% 且固实块不可部分解压） |
| A2 命令 | `python -m pytest tests -q` | **原命令已补跑：32 passed, 2 subtests**（0.49 s）；unittest 备选 32 OK | ✅ **已用原命令验证**（2026-11-09，`pa-b1-evidence-20261108/pytest.log`） |
| 模型体积 | 约 2 GB | SD1.5 实际 **3.97 GB** | 磁盘预算已核实（E 盘余 137 GB） |
| 版本 | 路线图锁定版本 | 0.3.40（master 的 comfy_kitchen 与 torch 2.5 不兼容） | **owner 2026-11-09 明确接受** |

**B1 结论：✅ 通过**。A1–A6 功能证据齐备；A2 已用**原命令 pytest** 验证（32 passed / 2 subtests）；
版本与实现路线（ComfyUI 0.3.40 源码）**已由 owner 于 2026-11-09 明确接受**，作为范围变更登记。

---

## 4. 硬门槛实测（基线 `ec656c6`）

| 项 | 命令 | 结果 | 原始输出 |
|---|---|---|---|
| 后端全量 | `.\scripts\test.ps1 backend` | 收集 **1661**，**PASS 1655**，6 条 skip，exit 0 | `pa-acceptance-20261108/backend.log` |
| 隔离审计 | `python scripts\run_tests.py audit` | PASS | 同次运行 |
| 前端 | `cd frontend; npx playwright test` | **71 passed**（1.1 m） | `pa-acceptance-20261108/playwright.log` |
| 端到端 | `python scripts\verify_e2e.py` | golden 10/10 + TLS 8/8 + collab PASS | `pa-acceptance-20261108/e2e.log` |
| 混合回归 | `python scripts\run_mixed_regression.py --rounds 10` | **10/10，红线 0，182.9 s** | `pa-acceptance-20261108/mixed.log` |
| mock 单测单独跑 | `python -m unittest tests.unit.test_mock_provider` | **Ran 6 tests，OK** | `pa-acceptance-20261108/mock-standalone.log` |
| GPU 保护自检 | `python scripts\verify_gpu_guard.py` | PASS（0 失败） | 见 STATUS §34 |
| 文档一致性 | `python scripts\check_docs_consistency.py` | PASS（3 项） | 见 STATUS §38 |

> 说明：**"1655 passed + 6 skipped" ≠ "1661 全过"**。6 条 mock 用例在受保护运行器下
> 因回环连接被阻断而 skip，独立运行时 6 条全过（上表两行分别给出）。

---

## 5. 部分通过明细（不粉饰）

### 5.1 v1.1 长素材：**已知限制**（2026-11-09 定案）

**结论：本机（6 GB 显存、CPU int8、faster-whisper）上，中文技术类长音频逐句转写 95% 达不到。**
这是实测上限，不是待办。详见 [`video-asr-limits.md`](video-asr-limits.md)。

| 素材 | 最好结果 |
|---|---|
| 合成 60 句（238.8 s） | large-v3 **56/60 = 93.3%**（5 种配置里最优） |
| 真人 43 句（5:04，合规） | **86.0%**（降噪后；原始 83.7%） |
| 真人 43 句（4:11，时长不足不计入） | 90.7% |

- 错误**全是同音字替换**（幂等→密等、阈值→预值、缓存块→缓存快、调度器→调度计）；
- 术语偏置、关 VAD、beam 10、关上下文继承**均无收益**；降噪只 +2.3 个点；
- **对外表述边界**：摘要与要点可用；逐字转写不可当逐字稿引用；
- 该限制**不影响 v1.1 其余判据**（短素材 95%、要点覆盖、时间戳、不截断均已通过）。

**不接受的做法**：LLM 后处理纠错（属于"猜"，不能计入转写准确率）。

### 5.2 v1.2 手机真机：未开始
本地 TLS/owner 桥接 8/8；真机 WLAN 与证书信任未验收。

### 5.3 v1.4 真实 Provider / 通知：未开始
模拟 Provider 只证明"本地链路通 + 不认自述"，**不等于** Kimi/DeepSeek 已接入，也不等于 QQ/ntfy 出站可用。

---

## 6. 证据索引（相对 EVIDENCE_ROOT）

| 内容 | 路径 |
|---|---|
| 本次基线证据（5 份原始日志 + meta） | `pa-acceptance-20261108/` |
| 总状态（38 节） | PROJECT_ROOT `docs/STATUS-20261107.md` |
| GPU 保护与事故记录 | PROJECT_ROOT `docs/gpu-protection.md` |
| v1.0 发布说明（含过程偏差 §3.4） | PROJECT_ROOT `docs/V1.0-RELEASE.md` |
| B1 运行时 A1–A6 | `pa-b1-evidence-20261108/b1-runtime.json` |
| 五链路快照（历史，`017bdfc`） | `pa-v17-evidence-20261108/summary.json` |
| 术语表实验 / large-v3 实验 | `pa-v1113-evidence-20261108/`、`pa-v1114-evidence-20261108/` |
| 金路径 / TLS | `pa-v125-evidence-20261108/`、`pa-v122-evidence-20261108/` |
| v1.0 各周 | `pa-w0…w8-evidence-20261107/` |

**历史快照与本次基线不同提交时，报告中已分别标注**，不再混用。

---

## 7. 风险与限制

| 风险 | 说明 |
|---|---|
| GPU 蓝屏 | 13:43 `0x116 VIDEO_TDR_FAILURE` 与 ComfyUI 出图同期；**显存压力是疑似诱因，确切根因未定位**。无论根因是否确认，四条限制继续生效（safe 启动、steps≤30、vram<2500 MB 拒绝、batch=1） |
| 未 push | 全部改动仅本地提交，push 需用户同意 |
| mock skip | 守卫阻断回环连接是基建边界，不是测试失效（§4 已给出两行证据） |
| 文档一致性脚本边界 | 只检查 3 项（Playwright 数、GPU 常量、启动脚本存在），**不代表所有叙述一致**；本次审查发现的路线图 W8 过期行已修 |
| 内存 | 16 GB；large-v3 占用更高，转写时避免同时重负载 |

---

## 8. 本轮根据审查做的修改

| 项 | 修改 |
|---|---|
| 状态收窄 | v1.0 改「部分通过（成果/过程）」；v1.3 与 v1.4a 改部分通过；v1.4b/c 单列未开始 |
| 过程偏差 | `V1.0-RELEASE.md` 新增 §3.4，并在 §8 结论披露；两套计次记录的矛盾已标注 |
| 基线统一 | 新增 `pa-acceptance-20261108/`（`ec656c6` 下 5 份原始日志），报告数字全部指向它 |
| B1 漂移 | §3.3 明列 portable→源码、pytest→unittest、模型 3.97 GB |
| GPU 措辞 | "显存压力导致 TDR" 收窄为"疑似诱因，根因未定位" |
| 证据根目录 | 报告头补 PROJECT_ROOT / ROADMAP_ROOT / EVIDENCE_ROOT |
| v1.0 限制 | `V1.0-RELEASE.md` §8.1 列 5 条对外表述边界 |

---

## 9. 结论与下一步

**成果层面**：v1.0 分数/来源/游戏/平台/故障达标；v1.1 短素材、v1.2 本地、v1.3 子集、v1.4a 本地与模拟、
B1 功能链、v1.5–v1.8 均有实测证据。

**不可签发的原因**：① W6 过程偏差；② 长素材 93.3%；③ 手机真机未验；④ 真实 Provider 未接入；
⑤ B1 命令/版本合规待确认。

**下一步顺序**：
1. owner 对 W6 偏差做一次明确表态（接受并记录 / 仍记部分通过）；
2. 补齐 B1 的 pytest 证据，或书面确认改用 unittest；
3. 真实 Provider 先用本地可得的只读通道探测（无 key 也可做能力快照），不把模拟当真实；
4. 手机与真人素材按资源到位后验收；
5. **不新增计划外功能**来掩盖上述缺口。
