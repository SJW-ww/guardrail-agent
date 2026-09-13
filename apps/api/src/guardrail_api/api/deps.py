"""路由依赖。"""

from typing import Annotated

from fastapi import Depends, Header

# 真实系统里这里读认证上下文。W1 先用请求头,方便前端与端到端测试指定操作者。
# 关键点不变:操作者来自调用方身份,**不由模型提供**。
DEFAULT_ACTOR = "operator-01"


async def get_actor(x_actor: Annotated[str | None, Header()] = None) -> str:
    return x_actor or DEFAULT_ACTOR


ActorDep = Annotated[str, Depends(get_actor)]
