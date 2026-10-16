"""检索片段的注入与引用（R4）。

这一层回答的是"检索到了，模型到底用上了没有"：

1. **预算**：注入的片段总长不超过 `MAX_CONTEXT_TOKENS`（约 1500 token）。
   超预算时按相关度截断，并在注入文本里写明"另有 N 条未展示"——
   不静默丢弃：模型得知道自己看到的不是全部。
2. **引用**：每个片段带 `[n]` 编号。`[n] → chunk_id` 的对应关系就是
   `knowledge_retrievals.top_chunk_ids` 的顺序（记的是**真正注入**的片段，
   不是检索到的前 k 条，否则截断之后编号就对不上了）；模型回复里的
   `[n]` 解析后写回 `cited_chunk_ids`，于是"引用命中率"可以离线复核，
   不必靠肉眼看回复（见 `scripts/run_knowledge_eval.py --citations`）。
3. **空检索不注入**：没有命中时 `text` 是空字符串，调用方不会往 messages
   里塞一段"知识库为空"的占位上下文，system prompt 里也不会出现
   "要求引用"的指令。
4. **没有身份不检索**：归属隔离必须下推到 SQL（见 `store.search_bm25`）。
   拿不到登录身份时调用方直接跳过检索，绝不回退到某个默认用户。

token 数按字符估算，不引入 tokenizer 依赖：CJK 字符按 1 token/字
（保守），其余字符按 4 字符/token。预算的意义是"不超"，不是"用满"。
"""

import re

from dataclasses import dataclass
from typing import Sequence

from src.knowledge.retrieve import retrieve
from src.knowledge.store import KnowledgeStore


MAX_CONTEXT_TOKENS = 1500
DEFAULT_K = 5

# 只认半角方括号 + 纯数字：[n]。
# 中文正文里常见的【n】不算引用，避免把正文内容误判成引用。
CITATION = re.compile(r"\[(\d{1,3})\]")

CJK = re.compile(
    "["
    "\u3000-\u303f"      # CJK 标点
    "\u3040-\u30ff"      # 日文假名
    "\u3400-\u4dbf"      # 扩展 A
    "\u4e00-\u9fff"      # 基本区
    "\uf900-\ufaff"      # 兼容区
    "\uac00-\ud7af"      # 韩文
    "]"
)

CONTEXT_HEADER = "知识库检索结果（与用户问题相关，按相关度排序）："

CONTEXT_NOTE = (
    "（因上下文预算，另有 {omitted} 条相关片段未展示；"
    "只引用上面出现过的编号。）"
)

TRUNCATED_MARK = "……（本片段因预算被截断）"

CITATION_RULE = (
    "下面可能给出从用户知识库检索到的片段，编号形如 [1]。"
    "用到其中内容时，用 [n] 标出依据，并且只标注真实出现过的编号；"
    "片段与问题无关就不要引用，也不要编造编号。"
)


def estimate_tokens(text: str) -> int:
    """估算 token 数：CJK 字符 1 token/字，其余 4 字符 ≈ 1 token。

    故意保守（宁可多估）：它的用途是"不超预算"。
    """

    if not text:
        return 0

    cjk = len(CJK.findall(text))

    return cjk + (len(text) - cjk + 3) // 4


def _fitting_prefix(text: str, budget: int) -> str:
    """返回不超过 budget 个 token 的最长前缀。"""

    if budget <= 0 or not text:
        return ""

    if estimate_tokens(text) <= budget:
        return text

    # 同一类字符的估算近似线性：先按比例定位，再逐字符收紧。
    # 片段本身很短，够快。
    cut = min(
        len(text),
        max(1, int(len(text) * budget / estimate_tokens(text))),
    )

    while cut > 0 and estimate_tokens(text[:cut]) > budget:
        cut -= 1

    return text[:cut]


def _render(number: int, hit: dict, body: str | None = None) -> str:
    """渲染一个片段：标题 + 来源 + 正文。"""

    title = str(hit.get("title") or "未命名文档").strip()
    url = str(hit.get("url") or "").strip()

    lines = [f"[{number}] {title}"]

    if url:
        lines.append(f"来源：{url}")

    lines.append(str(hit["text"]) if body is None else body)

    return "\n".join(lines)


def build_block(
    hits: Sequence[dict],
    *,
    max_tokens: int = MAX_CONTEXT_TOKENS,
) -> dict:
    """把命中的片段拼成注入块，并保证总长不超预算。

    返回 `{"text", "used", "omitted", "truncated", "tokens"}`：

    - `text` 为空字符串表示"这一轮不注入任何东西"；
    - `used` 是真正进了上下文的片段，顺序即 `[n]` 的编号；
    - `omitted` 是被预算挡在门外的条数（注入文本里会说明）。
    """

    if type(max_tokens) is not int or max_tokens < 1:
        raise ValueError("预算无效")

    hits = list(hits)

    # 说明行（"另有 N 条未展示"）也是注入的一部分，提前计入预算。
    # 预算的意义是"不超"，不是"用满"；宁可少注入一段，也不把总长顶出去。
    note_cost = estimate_tokens(
        CONTEXT_NOTE.format(omitted=99)
    )
    spent = estimate_tokens(CONTEXT_HEADER) + note_cost

    used = []
    rendered = []
    omitted = 0
    truncated = False

    for index, hit in enumerate(hits):
        number = len(used) + 1

        entry = _render(number, hit)
        cost = estimate_tokens(entry)

        if spent + cost <= max_tokens:
            used.append({**hit, "number": number})
            rendered.append(entry)
            spent += cost
            continue

        # 装不下：第一条尝试截断，其余全部算作未展示。
        remaining = max_tokens - spent

        if not used and remaining > 0:
            overhead = estimate_tokens(_render(number, hit, ""))
            body = _fitting_prefix(
                str(hit["text"]),
                remaining
                - overhead
                - estimate_tokens(TRUNCATED_MARK),
            )

            if body:
                used.append({**hit, "number": number})
                rendered.append(
                    _render(
                        number,
                        hit,
                        body + TRUNCATED_MARK,
                    )
                )
                truncated = True
                omitted = len(hits) - index - 1
                break

        omitted = len(hits) - index
        break

    parts = [CONTEXT_HEADER, *rendered]

    if omitted > 0:
        parts.append(CONTEXT_NOTE.format(omitted=omitted))

    text = "\n\n".join(parts)

    # 兜底：预算极小时，"另有 N 条未展示"这一行也可能把总量顶出去。
    # 逐个回退片段，直到真的不超预算；退光了就不注入。
    while used and estimate_tokens(text) > max_tokens:
        used.pop()
        rendered.pop()
        omitted += 1

        parts = [CONTEXT_HEADER, *rendered]

        if omitted > 0:
            parts.append(
                CONTEXT_NOTE.format(omitted=omitted)
            )

        text = "\n\n".join(parts)

    if not used:
        return {
            "text": "",
            "used": [],
            "omitted": len(hits),
            "truncated": False,
            "tokens": 0,
        }

    return {
        "text": text,
        "used": used,
        "omitted": omitted,
        "truncated": truncated,
        "tokens": estimate_tokens(text),
    }


def parse_citations(reply, count: int) -> list[int]:
    """取出回复里的 `[n]`：只保留 1..count，去重，按首次出现排序。"""

    if (
        not isinstance(reply, str)
        or type(count) is not int
        or count < 1
    ):
        return []

    found = []

    for match in CITATION.finditer(reply):
        number = int(match.group(1))

        if 1 <= number <= count and number not in found:
            found.append(number)

    return found


def map_citations(
    reply,
    hits: Sequence[dict],
) -> list[int]:
    """把 `[n]` 映射回 `chunk_id` —— 这就是"引用可回溯"。"""

    hits = list(hits)

    return [
        hits[number - 1]["chunk_id"]
        for number in parse_citations(reply, len(hits))
    ]


@dataclass(frozen=True)
class InjectedContext:
    """一次注入的完整结果：空检索时 `text` 与 `hits` 都是空的。"""

    text: str
    hits: tuple
    mode: str
    latency_ms: int
    omitted: int
    truncated: bool
    tokens: int
    store: KnowledgeStore
    retrieval_id: int | None

    @property
    def empty(self) -> bool:
        return not self.text

    def cited_chunk_ids(self, reply) -> list[int]:
        """回复里实际引用到的 chunk_id。"""

        return map_citations(reply, self.hits)

    def record_citations(self, reply) -> list[int]:
        """把回复里的 `[n]` 落回审计；没有引用就不写。"""

        cited = self.cited_chunk_ids(reply)

        if cited and self.retrieval_id is not None:
            self.store.record_citations(
                retrieval_id=self.retrieval_id,
                chunk_ids=cited,
            )

        return cited


def build_retrieval_context(
    *,
    owner_id: str,
    query: str,
    k: int = DEFAULT_K,
    store: KnowledgeStore | None = None,
    request_id: str | None = None,
    max_tokens: int = MAX_CONTEXT_TOKENS,
    bot_id: str | None = None,
) -> InjectedContext:
    """检索 → 组块 → 记审计，返回可直接注入的上下文。

    审计**每次都记**（包括空命中）：空结果本身也是"检索发生过"的证据。
    """

    store = store or KnowledgeStore()

    outcome = retrieve(
        owner_id=owner_id,
        query=query,
        k=k,
        store=store,
        record=False,
        bot_id=bot_id,
    )

    block = build_block(
        outcome["hits"],
        max_tokens=max_tokens,
    )

    retrieval_id = store.record_retrieval(
        user_id=owner_id,
        query=query,
        mode=outcome["mode"],
        k=k,
        latency_ms=outcome["latency_ms"],
        top_chunk_ids=[
            hit["chunk_id"] for hit in block["used"]
        ],
        request_id=request_id,
    )

    return InjectedContext(
        text=block["text"],
        hits=tuple(block["used"]),
        mode=outcome["mode"],
        latency_ms=outcome["latency_ms"],
        omitted=block["omitted"],
        truncated=block["truncated"],
        tokens=block["tokens"],
        store=store,
        retrieval_id=retrieval_id,
    )
