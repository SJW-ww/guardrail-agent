"""规划层:把人的意图变成结构化提议。

两条链路,同一个出口(`Proposal`),下游完全不需要知道这条提议是谁生成的:

- `deterministic`  规则解析,离线可跑,CI 默认走这条;
- `llm`            OpenAI 兼容模型,由 `PLANNER_BACKEND` 切换。

用哪条由配置决定,不由调用方决定 —— 规划器不能自己给自己降级。
模型不可用时**不会**偷偷退回规则规划器:审计里写着"模型提议"、
实际却是规则拼出来的,这种记录比没有记录更糟。
"""

from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.config import get_settings
from guardrail_api.planner.proposal import Proposal

__all__ = ["Proposal", "draft", "planner_backend"]


def planner_backend() -> Literal["deterministic", "llm"]:
    """当前生效的规划链路。llm 但凭据没配齐时,视为 deterministic。"""
    settings = get_settings()
    return "llm" if settings.llm_configured else "deterministic"


async def draft(session: AsyncSession, intent: str, *, actor: str) -> Proposal:
    """把一句自然语言变成一条可校验、可展示、可审批的提议。

    `actor` 不可省略:提议的执行档由执行体的信任等级决定,
    让调用方「忘了传」等于给了一条默认放行的后门。
    """
    if planner_backend() == "llm":
        # 延迟导入:没开 LLM 的部署不必为 httpx 之外的东西买单,
        # 也让「默认 deterministic」这条路径的导入链保持最短。
        from guardrail_api.planner.llm import LLMPlanner

        return await LLMPlanner().draft(session, intent, actor=actor)

    from guardrail_api.planner.deterministic import draft as deterministic_draft

    return await deterministic_draft(session, intent, actor=actor)
