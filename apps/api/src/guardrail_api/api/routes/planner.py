"""规划路由:把一句意图变成结构化计划,但**不执行**。"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.api.deps import ActorDep
from guardrail_api.db import get_session
from guardrail_api.planner import Plan, draft

router = APIRouter(prefix="/api/planner", tags=["planner"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class DraftProposalRequest(BaseModel):
    intent: str = Field(min_length=2, max_length=500, description="自然语言意图")


@router.post("/draft", response_model=Plan, summary="生成计划(不执行)")
async def draft_proposal(body: DraftProposalRequest, session: SessionDep, actor: ActorDep) -> Plan:
    """生成计划,但**不执行**。每一步都带策略引擎的裁决与理由 —— 提议卡片要能解释自己。"""
    return await draft(session, body.intent, actor=actor)
