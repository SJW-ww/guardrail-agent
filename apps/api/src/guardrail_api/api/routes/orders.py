"""订单 REST 路由。

视图模型直接复用工具层的定义:API 与 Agent 看到的是同一份资源表示,
避免两套 DTO 各自漂移。视图模型放在 tools/ 下是因为它首先是「工具返回什么」的契约。
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.db import get_session
from guardrail_api.domain.errors import NotFound
from guardrail_api.models import Order, OrderStatus
from guardrail_api.services import order as order_service
from guardrail_api.tools.order import OrderView, to_order_view

router = APIRouter(prefix="/api/orders", tags=["orders"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class OrderSummary(BaseModel):
    order_id: int
    order_no: str
    status: OrderStatus
    total_amount_cents: int
    customer_name: str
    item_count: int
    created_at: datetime


class OrderListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[OrderSummary]


class CancelOrderRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=128, description="取消原因,会写进订单与审计")


async def _committed_view(session: AsyncSession, order: Order) -> OrderView:
    await session.commit()
    return to_order_view(order)


@router.get("", response_model=OrderListResponse, summary="订单列表")
async def list_orders(
    session: SessionDep,
    status: OrderStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OrderListResponse:
    filters = [Order.status == status] if status is not None else []

    total = int(await session.scalar(select(func.count()).select_from(Order).where(*filters)) or 0)
    orders = list(
        await session.scalars(
            select(Order).where(*filters).order_by(Order.id.desc()).limit(limit).offset(offset)
        )
    )

    return OrderListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[
            OrderSummary(
                order_id=order.id,
                order_no=order.order_no,
                status=order.status,
                total_amount_cents=order.total_amount_cents,
                customer_name=order.customer.name,
                item_count=len(order.items),
                created_at=order.created_at,
            )
            for order in orders
        ],
    )


@router.get("/{order_id}", response_model=OrderView, summary="订单详情")
async def get_order_detail(order_id: int, session: SessionDep) -> OrderView:
    order = await session.scalar(select(Order).where(Order.id == order_id))
    if order is None:
        raise NotFound(f"订单 {order_id} 不存在")
    return to_order_view(order)


@router.post("/{order_id}/pay", response_model=OrderView, summary="支付订单")
async def pay_order(order_id: int, session: SessionDep) -> OrderView:
    order = await order_service.pay_order(session, order_id)
    return await _committed_view(session, order)


@router.post("/{order_id}/ship", response_model=OrderView, summary="发货")
async def ship_order(order_id: int, session: SessionDep) -> OrderView:
    order = await order_service.ship_order(session, order_id)
    return await _committed_view(session, order)


@router.post("/{order_id}/complete", response_model=OrderView, summary="完成订单")
async def complete_order(order_id: int, session: SessionDep) -> OrderView:
    order = await order_service.complete_order(session, order_id)
    return await _committed_view(session, order)


@router.post("/{order_id}/cancel", response_model=OrderView, summary="取消订单")
async def cancel_order(order_id: int, body: CancelOrderRequest, session: SessionDep) -> OrderView:
    order = await order_service.cancel_order(session, order_id, reason=body.reason)
    return await _committed_view(session, order)
