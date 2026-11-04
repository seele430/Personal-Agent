"""trace 上下文：进程内传递 trace_id / span_id。

两条规则：
1. **只存 id，不存对象** —— contextvar 里放字符串最安全。
2. **所有写入都吞异常** —— 观测代码不能把业务打挂。

跨线程提醒：`ThreadPoolExecutor` 不会自动复制 contextvar，
需要在提交任务时用 `contextvars.copy_context().run(...)`；
asyncio 任务与 FastAPI 的同步端点（anyio 线程池）会自动继承。
"""

import logging

import time

import uuid

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

logger = logging.getLogger("personal_agent.trace")


_trace_id: ContextVar[str | None] = ContextVar(
    "trace_id",
    default=None,
)

_span_id: ContextVar[str | None] = ContextVar(
    "span_id",
    default=None,
)


def new_trace_id() -> str:
    """32 位十六进制，与 W3C trace-id 同形。"""

    return uuid.uuid4().hex


def new_span_id() -> str:
    """16 位十六进制，与 W3C span-id 同形。"""

    return uuid.uuid4().hex[:16]


def current_trace_id() -> str | None:

    return _trace_id.get()


def current_span_id() -> str | None:

    return _span_id.get()


class SpanHandle:
    """手动控制结束时机（HTTP 中间件需要按状态码定 status）。"""

    def __init__(
        self,
        *,
        name: str,
        trace_id: str,
        span_id: str,
        parent_id: str | None,
        started: float,
        attrs: dict,
        trace_token: Token | None,
        span_token: Token,
    ):
        self.name = name
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_id = parent_id
        self.attrs = attrs

        self._started = started
        self._trace_token = trace_token
        self._span_token = span_token
        self._ended = False

    def end(self, status: str = "ok") -> None:
        """幂等：调用两次只记一条。"""

        if self._ended:
            return

        self._ended = True

        dur_ms = time.monotonic() * 1000.0 - self._started

        _span_id.reset(self._span_token)

        if self._trace_token is not None:
            _trace_id.reset(self._trace_token)

        try:
            from src.core.trace_store import get_trace_store

            get_trace_store().record_span(
                trace_id=self.trace_id,
                span_id=self.span_id,
                parent_id=self.parent_id,
                name=self.name,
                start_ms=self._started,
                dur_ms=dur_ms,
                status=status,
                attrs=self.attrs,
            )

        except Exception:
            logger.exception(
                "span 写入失败 trace_id=%s name=%s",
                self.trace_id,
                self.name,
            )


def start_span(name: str, **attrs) -> SpanHandle:
    """开始一个 span。没有 trace 时自动开一条（后台任务/脚本）。"""

    trace_id = _trace_id.get()
    trace_token = None

    if trace_id is None:
        trace_id = new_trace_id()
        trace_token = _trace_id.set(trace_id)

    parent_id = _span_id.get()
    span_id = new_span_id()

    span_token = _span_id.set(span_id)

    return SpanHandle(
        name=name,
        trace_id=trace_id,
        span_id=span_id,
        parent_id=parent_id,
        started=time.monotonic() * 1000.0,
        attrs=attrs,
        trace_token=trace_token,
        span_token=span_token,
    )


@contextmanager
def span(name: str, **attrs) -> Iterator[str]:
    """带 with 的写法：出异常自动记 error。"""

    handle = start_span(name, **attrs)

    try:
        yield handle.span_id

    except Exception:
        handle.end("error")
        raise

    else:
        handle.end("ok")


@contextmanager
def bind_trace(trace_id: str) -> Iterator[str]:
    """把后续代码绑到指定 trace 上（入口中间件用）。"""

    trace_token = _trace_id.set(trace_id)
    span_token = _span_id.set(None)

    try:
        yield trace_id

    finally:
        _span_id.reset(span_token)
        _trace_id.reset(trace_token)
