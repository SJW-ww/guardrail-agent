"""统一时间入口。

所有服务都从这里取时间,不在业务代码里直接调 datetime.now ——
这样后续要做「可控时钟」或重放历史事件时,只需要换这一个地方。
"""

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)
