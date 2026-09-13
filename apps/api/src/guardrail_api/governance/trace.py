"""trace_id 的贯穿。

从 HTTP 入口生成(或透传调用方给的),一路带到审计行。
审计的价值一半在于「能串起来」—— 一次请求触发的所有写操作必须挂同一个 trace_id,
否则出了事只能在日志里肉眼拼时间线。
"""

import contextvars
import uuid

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "guardrail_trace_id", default=None
)


def new_trace_id() -> str:
    return uuid.uuid4().hex


def set_trace_id(trace_id: str) -> contextvars.Token[str | None]:
    return _trace_id.set(trace_id)


def reset_trace_id(token: contextvars.Token[str | None]) -> None:
    _trace_id.reset(token)


def get_trace_id() -> str | None:
    """当前上下文里的 trace_id;没有就返回 None,由调用方决定兜底策略。"""
    return _trace_id.get()
