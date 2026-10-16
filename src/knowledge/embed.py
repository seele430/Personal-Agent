"""向量化（R3）：把片段映射成稠密向量，并保证"同一段内容只算一次"。

设计约束（来自 `docs/MEMORY_RAG_PLAN.md` 的 R3）：

1. **可插拔**：上层只依赖 `Embedder`（`name` / `dim` / `embed(texts)`）。

   - `HashingEmbedder`：确定性、零依赖、零网络。它只是**词法**向量
     （字符 n-gram 哈希），**不是语义向量** —— 它的价值是让测试与离线评测
     在没有任何模型/密钥时也能跑通整条链路，并充当"词法向量"这一档的对照。
   - `OpenAICompatibleEmbedder`：任何 OpenAI 兼容的 `/embeddings` 服务。
     ⚠️ 实测（2026-10-15）：本机配的 DeepSeek `https://api.deepseek.com`
     **没有 embeddings 端点**（`/embeddings` 返回 404，模型只有
     `deepseek-flash` / `deepseek-v4-pro`），所以这条路必须指向别家
     （硅基流动 / 阿里百炼 / Jina / OpenAI…），或者改用本地模型。
   - `SentenceTransformerEmbedder`：本地句向量模型（可选依赖，见
     `requirements-embed.txt`），默认 `BAAI/bge-small-zh-v1.5`。

2. **内容级缓存**：缓存键是 `(model, sha256(片段文本))`，落在
   `knowledge_embedding_cache`。同一段文本无论出现在哪个文档、哪个用户下，
   都只算一次 —— 这是"重复摄取零新增计算"的依据，也是 embedding 真正花钱
   的地方。注意缓存是**模型级**的（不含 owner）：它存的是文本的确定性函数，
   检索依旧按 owner 收窄，不存在跨用户召回；泄露面仅限于"持有数据库的人
   可以验证某段文本是否被算过"，而持有数据库本身已经是完全访问。

3. **落盘为 float32 BLOB**：先不引向量库（计划里写明：等有实测延迟数字再
   决定）。检索时只取该 owner 的向量做暴力 cosine —— 隔离下推到候选生成，
   不是"先取全局 top-k 再过滤"。

4. **不引 numpy**：venv 里没有 numpy，向量用 `array('f')` 打包、相似度用
   纯 Python 点积。暴力扫描的真实延迟会被评测如实测出来 —— 这正是
   "当前量级要不要引向量库"这个取舍的证据。
"""

import math

from array import array
from openai import OpenAI

from src.knowledge.store import KnowledgeStore, content_hash


DEFAULT_HASHING_DIM = 256
DEFAULT_HASHING_NGRAM = 2

DEFAULT_LOCAL_MODEL = "BAAI/bge-small-zh-v1.5"

PROVIDERS = ("hashing", "openai", "local")


# ==========================================
# 向量与 BLOB 互转
# ==========================================

def normalize_vector(vector) -> list[float]:
    """L2 归一化；零向量原样返回（不做除零）。"""

    norm = math.sqrt(
        sum(value * value for value in vector)
    )

    if norm == 0.0:
        return [float(value) for value in vector]

    return [float(value) / norm for value in vector]


def vector_blob(vector) -> bytes:
    """float32 小端打包。float32 是省空间与精度之间的折中。"""

    return array("f", [float(value) for value in vector]).tobytes()


def blob_vector(blob: bytes) -> list[float]:
    values = array("f")
    values.frombytes(blob)

    return [float(value) for value in values]


def cosine(left, right) -> float:
    """余弦相似度。两边若已归一化，这就是点积。"""

    if len(left) != len(right):
        raise ValueError("向量维度不一致")

    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0

    for a, b in zip(left, right):
        dot += a * b
        left_norm += a * a
        right_norm += b * b

    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0

    return dot / math.sqrt(left_norm * right_norm)


def _stable_hash(text: str) -> int:
    """FNV-1a：跨进程稳定。

    不能用内置 hash() —— 它按 PYTHONHASHSEED 随机化，会让"确定性向量"
    在两次运行之间变掉，缓存与评测就全不可复现了。
    """

    value = 2166136261

    for char in text:
        value ^= ord(char)
        value = (value * 16777619) & 0xFFFFFFFF

    return value


# ==========================================
# Embedder 接口与三种实现
# ==========================================

class Embedder:
    """向量化接口：只要求 name / dim / embed。"""

    name = "embedder"
    dim = 0

    def embed(self, texts) -> list:
        raise NotImplementedError

    def embed_one(self, text: str) -> list[float]:
        vectors = self.embed([text])

        if len(vectors) != 1:
            raise RuntimeError("embedder 返回的向量条数不正确")

        return vectors[0]


class HashingEmbedder(Embedder):
    """词法向量：字符 n-gram 哈希 + L2 归一化。

    **不是语义向量**：它只会让"字符片段重叠"的文本相似，
    用来在没有模型/密钥时跑通链路，并作为词法向量对照。
    """

    def __init__(
        self,
        dim: int = DEFAULT_HASHING_DIM,
        ngram: int = DEFAULT_HASHING_NGRAM,
    ):
        if type(dim) is not int or dim < 8:
            raise ValueError("维度无效")

        if type(ngram) is not int or ngram < 1:
            raise ValueError("n-gram 长度无效")

        self.dim = dim
        self.ngram = ngram
        self.name = f"hashing-{ngram}gram-{dim}"

    def _grams(self, text: str):
        text = " ".join(text.split()).lower()

        if len(text) < self.ngram:
            if text:
                yield text
            return

        for start in range(len(text) - self.ngram + 1):
            yield text[start:start + self.ngram]

    def embed(self, texts) -> list:
        vectors = []

        for text in texts:
            vector = [0.0] * self.dim

            for gram in self._grams(text):
                index = _stable_hash(gram) % self.dim
                vector[index] += 1.0

            vectors.append(normalize_vector(vector))

        return vectors


class OpenAICompatibleEmbedder(Embedder):
    """任何 OpenAI 兼容的 /embeddings 服务。

    DeepSeek 官方没有这个端点（实测 404），所以要显式指向别家。
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        batch_size: int = 32,
        timeout: float = 30.0,
    ):
        if not model or not api_key or not base_url:
            raise ValueError("model / api_key / base_url 都不能为空")

        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size 无效")

        self.name = f"openai:{model}"
        self.model = model
        self.batch_size = batch_size
        self.dim = 0

        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
        )

    def embed(self, texts) -> list:
        vectors = []

        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start:start + self.batch_size])

            response = self._client.embeddings.create(
                model=self.model,
                input=batch,
            )

            ordered = sorted(
                response.data,
                key=lambda item: item.index,
            )

            for item in ordered:
                vector = [float(value) for value in item.embedding]

                if self.dim == 0:
                    self.dim = len(vector)
                elif len(vector) != self.dim:
                    raise RuntimeError(
                        "同一个模型返回了不同维度"
                    )

                vectors.append(vector)

        return vectors


class SentenceTransformerEmbedder(Embedder):
    """本地句向量模型（可选依赖，见 requirements-embed.txt）。

    装不上时给出明确指引，而不是让整条链路莫名其妙失败。
    """

    def __init__(self, model_name: str = DEFAULT_LOCAL_MODEL):
        try:
            from sentence_transformers import (
                SentenceTransformer,
            )
        except ImportError as error:  # pragma: no cover
            raise RuntimeError(
                "未安装 sentence-transformers，本地向量不可用。"
                "请先执行："
                "pip install -r requirements-embed.txt "
                "-i https://mirrors.aliyun.com/pypi/simple/ ，"
                "并设置 HF_ENDPOINT=https://hf-mirror.com"
            ) from error

        self.name = f"local:{model_name}"
        self.model_name = model_name
        self.dim = 0

        self._model = SentenceTransformer(model_name)

        getter = (
            getattr(self._model, "get_embedding_dimension", None)
            or getattr(
                self._model, "get_sentence_embedding_dimension", None
            )
        )

        try:
            self.dim = int(getter()) if getter else 0
        except Exception:  # pragma: no cover
            self.dim = 0

    def embed(self, texts) -> list:  # pragma: no cover
        vectors = self._model.encode(
            list(texts),
            normalize_embeddings=True,
        )

        rows = [
            [float(value) for value in vector]
            for vector in vectors
        ]

        if self.dim == 0 and rows:
            self.dim = len(rows[0])

        return rows


def build_embedder(
    provider: str | None = None,
    *,
    environ=None,
) -> Embedder:
    """按配置挑一个 embedder。

    优先级：显式参数 > 环境变量 > 兜底 hashing。

    环境变量：
        EMBEDDING_PROVIDER  hashing | openai | local
        EMBEDDING_MODEL     模型名（openai / local）
        EMBEDDING_BASE_URL  OpenAI 兼容服务的地址
        EMBEDDING_API_KEY   该服务的密钥
    """

    import os

    env = os.environ if environ is None else environ

    chosen = (
        provider
        or env.get("EMBEDDING_PROVIDER", "")
    ).strip().lower()

    if not chosen:
        # 没有显式指定：有 key 就走 API，否则退回确定性词法向量
        chosen = (
            "openai"
            if env.get("EMBEDDING_API_KEY", "").strip()
            else "hashing"
        )

    if chosen not in PROVIDERS:
        raise ValueError(
            f"未知的 embedding provider：{chosen!r}，"
            f"可用：{list(PROVIDERS)}"
        )

    if chosen == "hashing":
        return HashingEmbedder()

    if chosen == "local":
        return SentenceTransformerEmbedder(
            env.get("EMBEDDING_MODEL", "").strip()
            or DEFAULT_LOCAL_MODEL
        )

    return OpenAICompatibleEmbedder(
        model=env.get("EMBEDDING_MODEL", "").strip()
        or "BAAI/bge-m3",
        api_key=env.get("EMBEDDING_API_KEY", "").strip(),
        base_url=env.get("EMBEDDING_BASE_URL", "").strip()
        or "https://api.siliconflow.cn/v1",
    )


# ==========================================
# 建索引：只补缺，同内容只算一次
# ==========================================

def index_embeddings(
    *,
    owner_id: str,
    store: KnowledgeStore | None = None,
    embedder: Embedder | None = None,
    batch_size: int = 32,
) -> dict:
    """给该 owner 还没有向量的片段补上向量。

    返回 `{"embedded", "cached", "chunks"}`：
    - `embedded`：真正调用过 embedder 的**去重文本**条数；
    - `cached`：命中内容缓存、没再算的条数；
    - `chunks`：这次补上向量的片段数。

    只处理"缺的"：重复调用第二次就是 0 计算（幂等）。
    """

    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("batch_size 无效")

    store = store or KnowledgeStore()
    embedder = embedder or build_embedder()

    pending = store.chunks_missing_embedding(
        owner_id=owner_id,
        model=embedder.name,
    )

    if not pending:
        return {"embedded": 0, "cached": 0, "chunks": 0}

    # 同一段文本在这个 owner 内可能出现多次：先按内容去重
    by_digest = {}

    for row in pending:
        by_digest.setdefault(
            content_hash(row["text"]),
            row["text"],
        )

    cached = store.embedding_cache_get(
        model=embedder.name,
        digests=list(by_digest),
    )

    missing = [
        digest
        for digest in by_digest
        if digest not in cached
    ]

    fresh = {}

    for start in range(0, len(missing), batch_size):
        batch = missing[start:start + batch_size]

        vectors = embedder.embed(
            [by_digest[digest] for digest in batch]
        )

        if len(vectors) != len(batch):
            raise RuntimeError(
                "embedder 返回的向量条数不正确"
            )

        for digest, vector in zip(batch, vectors):
            if not vector:
                raise RuntimeError("embedder 返回了空向量")

            fresh[digest] = (
                len(vector),
                vector_blob(normalize_vector(vector)),
            )

    if fresh:
        store.save_embedding_cache(
            model=embedder.name,
            entries=[
                (digest, dim, blob)
                for digest, (dim, blob) in fresh.items()
            ],
        )

    merged = {**cached, **fresh}

    rows = [
        (
            row["chunk_id"],
            embedder.name,
            content_hash(row["text"]),
            *merged[content_hash(row["text"])],
        )
        for row in pending
    ]

    store.save_chunk_embeddings(rows)

    return {
        "embedded": len(fresh),
        "cached": len(by_digest) - len(fresh),
        "chunks": len(rows),
    }
