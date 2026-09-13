"""路由依赖。"""

from typing import Annotated

from fastapi import Depends, Header

# 真实系统里这里读认证上下文。W1 先用请求头,方便前端与端到端测试指定操作者。
# 关键点不变:操作者来自调用方身份,**不由模型提供**。
DEFAULT_ACTOR = "operator-01"


async def get_actor(x_actor: Annotated[str | None, Header()] = None) -> str:
    return x_actor or DEFAULT_ACTOR


def normalize_actor(raw: str) -> str:
    """把请求头里的操作者规范成**执行体身份**,和工具网关的 `human:` 口径对齐。

    已经带前缀的原样返回 —— `agent:` 前缀必须能被保留下来,
    否则策略引擎就认不出"这是机器在做决定"。这个区分是审批规则的前提。
    """
    return raw if ":" in raw else f"human:{raw}"


ActorDep = Annotated[str, Depends(get_actor)]
