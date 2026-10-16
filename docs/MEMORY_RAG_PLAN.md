# 长期记忆 / 知识库（RAG）规划

> 对应 `DEVELOPMENT_PLAN.md` 的 **M7（短期摘要 + 长期检索记忆）**。
> 本文把 M7 拆成可验收的增量，并说明每一步**产出什么可验证的数字**。

## 0. 一句话

让 Personal Agent 从"提醒 + 监控工具"变成**有记忆的个人知识与自动化中枢**：
监控抓回来的正文、用户上传的资料、历史会话摘要，都进知识库，回答问题时先检索再生成。

---

## 1. 现状（为什么这件事现在才做）

| 事实 | 证据 |
| --- | --- |
| 每次聊天请求的上下文**只有 system + 当前这一条消息** | `src/agent.py:693-704` |
| 会话历史只用于**界面恢复**，不回灌模型 | 前端 `sessionStorage` 键 `personal-agent-user-<id>-chat` |
| 监控抓到的内容**只存标题和链接，不存正文** | `src/core/task_engine.py` 的 `seen_items(monitor_id, item_id, title, link, seen_at)` |
| 计划里已写明方向：**相关记忆检索（PostgreSQL + pgvector），不把全部历史发送到模型** | `DEVELOPMENT_PLAN.md:40`，M7 同 |
| 计划文档自己把"长期记忆"列在**尚未验证**清单里 | `DEVELOPMENT_PLAN.md:59` |

**产品动机（不是技术堆料）**：现在监控能"告诉你发生了什么"，但**答不上"上周那篇讲了什么"** ——
正文抓回来就丢了。把正文沉淀进知识库，"监控"就从事后通知升级为可检索的知识资产。
这是这个项目**独有**的知识来源：别人的 RAG demo 喂的是 PDF，我们喂的是**持续自动更新的订阅源**。

---

## 2. 范围与非范围

**做**：单机、SQLite 优先、中文可用、按 `owner_id`/`bot_id` 隔离、可离线评测、成本可计量。

**不做**（写清楚，避免面试被问到自己挖的坑）：

- 不做通用文档问答 SaaS，不做多租户云服务；
- **不引入 LangChain / LlamaIndex** —— 手写检索链路（与 DeepResearch 的"手写 ReAct 循环"同一取舍），
  理由是依赖治理与可控性：检索隔离必须下推到 SQL，融合与评测口径要自己掌握；
- **先不上向量库**（Chroma/Milvus/pgvector）：量级不到就先暴力 cosine，**等有实测延迟数字再决定**（见 R3）；
- 不做 rerank 模型（先 RRF 融合，等有对照数据再说）；
- 不把全量历史塞进索引（用摘要，见 R1 的 `source_type='summary'`）。

---

## 3. 架构

### 3.1 表结构（SQLite 阶段）

```sql
-- 文档：一篇 = 一个可引用的来源
knowledge_documents (
    id INTEGER PRIMARY KEY,
    owner_id TEXT NOT NULL,
    bot_id   TEXT,                    -- 计划里的 Bot 专属记忆，可空
    source_type TEXT NOT NULL         -- 'monitor' | 'upload' | 'summary'
        CHECK (source_type IN ('monitor','upload','summary')),
    source_ref TEXT,                  -- monitor 的 item_id / 文件名 / request_id
    title TEXT, url TEXT,
    sha256 TEXT NOT NULL,             -- 内容级幂等键
    created_at TEXT NOT NULL,
    UNIQUE (owner_id, sha256)         -- 同一用户重复摄取同一内容 → 只留一份
)

-- 片段：检索与引用的最小单位
knowledge_chunks (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL
        REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,             -- 文档内序号：引用 [n] 可回溯到具体片段
    text TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    UNIQUE (document_id, seq)
)

-- 检索审计（仿 task_run_history 的思路）
knowledge_retrievals (
    id INTEGER PRIMARY KEY,
    request_id TEXT, user_id TEXT NOT NULL,
    query_sha256 TEXT NOT NULL,       -- 不落明文 query，避免日志里泄露隐私
    mode TEXT NOT NULL,               -- 'bm25' | 'vector' | 'hybrid'
    k INTEGER NOT NULL, latency_ms INTEGER NOT NULL,
    top_chunk_ids TEXT,               -- JSON 数组，回答引用是否命中靠它核对
    created_at TEXT NOT NULL
)
```

**隔离是结构性的**：`owner_id` 在 `knowledge_documents` 上，检索时**必须先按 owner 收窄候选集**
（见 3.3 的坑）。

### 3.2 模块划分

```
src/knowledge/
    chunker.py     # 分块：结构优先（标题/段落）→ 固定窗口兜底 + overlap
    ingest.py      # 摄取：monitor 正文 / 上传文件 / 会话摘要 → documents + chunks
    embed.py       # 向量化：批处理 + 落盘缓存（同一 chunk 不重复算）
    retrieve.py    # 检索：bm25 / vector / hybrid(RRF)，owner 过滤下推
    store.py       # SQLite 读写：documents / chunks / retrievals
```

**注入点**：`src/agent.py` 组装 `messages` 的地方（`:693`），在 system 之后插入检索片段，
并要求模型用 `[1]`/`[2]` 引用；**检索为空则不注入**。

### 3.3 ⚠️ 两个必须提前想清楚的坑

**坑一：隔离不能"先取全局 top-k 再过滤"。**
若先在全库取前 50 条再筛 `owner_id`，属于该用户的文档可能排在 200 名外 —— **召回塌陷**；
而且一旦有 bug 就把别人的内容喂进了上下文。**必须在候选生成时下推**：
BM25 走 `FTS5 join knowledge_documents WHERE owner_id = ?`；向量走"先取该 owner 的 chunk 子集再算相似度"。

**坑二：FTS5 默认不分中文词。**
`unicode61` 分词器会把一整句中文当成一个 token → 中文 BM25 基本失效。
可选：(a) `tokenize='trigram'`（SQLite 3.34+，中文效果好、索引大）；
(b) 自建按字/双字切分后写入影子列。**这条要写进文档并在评测里给出对比数字**。

---

## 4. 增量序列（每个都能单独验收）

### R1 摄取与索引 —— 建库、幂等、隔离
- **做什么**：三张表 + `chunker.py` + `ingest.py`；监控抓取时把正文落进知识库（同时仍写 `seen_items`）；
  上传接口（md/txt 起步）；`sha256` 去重。
- **验收（可断言）**：同一文档重复摄取 → `knowledge_documents` 仍只有 1 行；
  A 用户的 `owner_id` 查不到 B 用户的 chunk；删文档 → chunk 级联删除。
- **代价**：约 1 小时。

### R2 BM25 基线 —— 先有"没做向量时能到多少"
- **做什么**：FTS5 索引（`trigram`）+ `retrieve(mode='bm25')`。
- **验收**：`evals/knowledge/cases.jsonl`（40 条 query → 应命中的 doc）跑出 **recall@1/3/5 + MRR**，
  记录 **P50/P95 延迟**。**这一步的数字是后面所有提升的基准线。**
- **代价**：约 1 小时。

### R3 向量 + 融合 —— 拿到"提升了多少"的对照 ✅ 已完成（PR #15 已合并，实测见 §4.5）
- **做什么**：`embed.py`（批处理 + 缓存；embedding 走 DeepSeek/OpenAI 兼容接口，或本地小模型二选一，
  用**成本与延迟实测**决定）；向量存 BLOB（float32）；`mode='vector'` 用纯 Python cosine；
  `mode='hybrid'` 用 **RRF**（`score = Σ 1/(60+rank)`，比加权和免调参、更稳）。
- **验收**：三组数字对照表（BM25 / 向量 / RRF），含 recall@5、MRR、P95 延迟、**每查询成本**
  （直接复用 `ai_usage_calls` 的计量）；并给出一条**明确的取舍结论**
  （例如"暴力 cosine 在 N 万 chunk 下 P95 = X ms，因此暂不引向量库"）。
- **代价**：约 2 小时（主要是 embedding 的批处理与缓存）。

### R4 注入与引用 —— 让它真的被用上
- **做什么**：`agent.py` 注入片段；预算上限（片段总长 ≤ 约 1500 tokens，超了截断并向模型说明）；
  system 里要求引用 `[n]`；`[n] → chunk_id` 记入 `knowledge_retrievals`。
- **验收**：检索为空时**断言 messages 里没有检索段**；有命中时断言 `[n]` 与 chunk 一一对应；
  引用命中率（生成的 `[n]` 是否指向 golden 期望的 doc）纳入评测。
- **代价**：约 1 小时。

### R5 观测与运维 —— 能删、能过期、能重索引 ✅ 已完成（2026-10-16）

实现（`src/knowledge/store.py`）：

- **软过期**：`knowledge_documents.expires_at`（NULL = 永不过期）+ `set_expiry()`；
  三条检索路径（FTS / LIKE / 向量）统一加
  `AND archived = 0 AND (expires_at IS NULL OR expires_at > now)`；
  过期后检索不到但记录仍在，清空即恢复。
- **归档（重索引）**：同 `(owner_id, source_ref)` 的新内容进来，旧版本 `archived = 1`
  并从检索候选剔除；`list_documents(include_archived=True)` 仍可见（可复核 / 回滚）。
- **检索审计 + 删除**：R4 / R1 已落地（`record_retrieval` / `list_retrievals` /
  `delete_document` 级联）。

验收（`tests/test_knowledge_r5.py`，11 条断言，全绿）：过期后检索不到（BM25 与向量
双路径）、清空过期恢复、跨 owner 无副作用、同 source_ref 新版本归档旧版本、幂等摄取
不误归档、归档跨 source_ref / owner 隔离。

**合计约 6 小时**，建议分 2–3 次会话；R1+R2 先做完，就有可演示、可讲的东西了。

## 4.5 R3 实测结果（2026-10-16，真语义向量）

同一批 40 文档 / 66 query / known-item 评测，两版向量对照：

| 通道 | recall@1 | recall@5 | MRR | P95 |
|---|---|---|---|---|
| BM25（FTS5 trigram） | 0.9545 | 0.9848 | 0.9672 | 5 ms |
| 词法向量（hashing-2gram-256，零依赖兜底） | 0.7273 | 0.9545 | 0.8277 | 5 ms |
| **语义向量（bge-small-zh-v1.5，本地）** | **0.8333** | 0.9394 | 0.8756 | 24 ms |
| hybrid（BM25 + 语义向量，RRF） | 0.9394 | 0.9848 | 0.9551 | 29 ms |

**取舍结论（不是"向量没用"，是"当前量级用不上"）：**

1. 语义向量确实强于词法哈希（vector 的 recall@1 从 0.7273 → 0.8333），语义召回能力真实存在；
2. 但**这个评测集是 known-item 字面匹配**（query 从文档自身稀有词派生，见 §5），BM25 已接近天花板（recall@1 0.9545），向量与 hybrid **都没能超越它**；
3. 语义向量 P95 24 ms、hybrid 29 ms，是 BM25（5 ms）的 5–6 倍，却无召回增益 → **当前单机 SQLite 量级，不上向量库、默认走 BM25，是被数据支持的决定**；
4. 语义向量的真实价值（语义改写、跨语言、开放域问答）**这个评测集测不出来**——要证明它，得补"语义改写 query"或开放域评测，属后续工作，不在 R3 范围。

成本口径：本地 bge-small 零 API 费用（一次 encode 61 段，秒级）；换 API embedding 才有费用（见 `embed.py` 的 `OpenAICompatibleEmbedder`）。

---

## 5. 评测计划（这是简历上最有说服力的部分）

复用现有评测形态（`src/evals/reminder_eval.py` + `evals/reminder/cases.jsonl` + 结果 JSON）：

```
evals/knowledge/cases.jsonl
  {"id":"kb_01","category":"monitor","query":"上周那篇讲 asyncio 的文章说了什么",
   "expect_doc_ids":["doc_9f3a"],"must_cite":true}
结果 JSON：{started_at, model, strategy, summary:{recall_at_1, recall_at_5, mrr,
           citation_hit_rate, p95_latency_ms, cost_usd}, cases:[...]}
```

**报告的四个数字**（面试时按这个顺序讲）：

| 指标 | 说明 | 为什么面试官在意 |
| --- | --- | --- |
| recall@5 | 期望文档是否进前 5 | 检索质量的根本指标，且能横向对比 BM25/向量/融合 |
| MRR | 期望文档的平均排名倒数 | 反映"排得够不够前"，比 recall 更敏感 |
| 引用命中率 | 生成文本里的 `[n]` 是否指向正确文档 | **证明不是"检索了但模型没用"** |
| P95 延迟 + 每查询成本 | 检索耗时与 embedding 花费 | 体现工程约束，不是纯算法炫技 |

**对照实验**：同一批 query 跑 `bm25` / `vector` / `hybrid` 三组 —— 一张表就是一段好故事。

---

## 6. 简历话术模板（数字待填，做完 R1–R5 后替换）

> 直接可用，**不要**在数字填真实值之前写进简历。

- 为 Personal Agent 设计并实现**检索增强的长期记忆**：SQLite + FTS5(BM25) 与向量**双通道召回**，
  RRF 融合。离线评测（40 文档 / 66 query）实测：BM25 recall@5 = 0.9848 已是天花板，
  语义向量（bge-small-zh 本地模型）recall@1 = 0.8333、P95 24 ms，hybrid 融合后 recall@5 = 0.9848
  **无召回增益** → 用数据否决"当前量级上向量库"，默认走 BM25。
- 把**监控抓取的网页正文**沉淀为可检索知识（**__ 篇 / __ 个片段**），使"监控"从通知升级为知识资产；
  摄取按内容 `sha256` **幂等**，重复摄取零新增。
- 检索**隔离下推到候选生成阶段**（`owner_id`/`bot_id`），避免"先取全局 top-k 再过滤"造成的
  召回塌陷与越权风险；配套 **__ 条断言**（跨用户不可见 / 空检索不注入 / 引用可回溯 / 删除级联）。
- 建立**离线评测集**（**__ 条 golden query**）量化检索质量与成本：三组对照（BM25 / 向量 / RRF）
  + 引用准确率 + 每查询成本 **$__**，并据此给出"当前量级不引入向量库"的取舍结论。

---

## 7. 面试会被追问的 6 个问题（含答法要点）

| 问题 | 答法要点（都落在本仓库已有证据上） |
| --- | --- |
| 片段怎么切？为什么？ | 结构优先（按标题/段落）→ 固定窗口兜底 + overlap；给出两种切法的 recall 对照，而不是拍脑袋 |
| 为什么不用 LangChain / 向量库？ | 规模（实测暴力 cosine 的 P95）+ 依赖治理（本机踩过插件依赖导致的启动事故）+ 检索隔离必须下推 SQL，框架反而挡路 |
| 召回率怎么测的？ | 40 条 golden query + recall@k/MRR + **引用命中率**（区分"检索到"与"用上了"） |
| 怎么防幻觉？ | 空检索不注入；强制 `[n]` 引用；`[n]` 可回溯到 `chunk_id`；检索审计可复核 |
| 中文检索的坑？ | FTS5 `unicode61` 不分中文词 → `trigram` 或自切；embedding 对中文的实测效果对比 |
| 隐私与隔离？ | 下推隔离、按 owner 收窄候选、query 只存 `sha256`、检索审计（谁何时检索了什么） |

---

## 8. 已知边界（诚实版）

- SQLite 阶段不适合高并发写：摄取是**离线/后台**动作，不在请求路径上。
- embedding 会产生**真实费用**：必须做批处理 + 内容 `sha256` 缓存，避免重复计算。
- golden set 是**我自己构造**的，存在偏差；规模到 40 条只能说明趋势，不能当学术评测。
- 摘要型记忆（`source_type='summary'`）会**丢细节**，与原文检索是互补而非替代。
