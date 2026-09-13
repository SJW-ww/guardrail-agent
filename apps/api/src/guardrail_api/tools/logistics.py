from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from guardrail_api.integrations.logistics import TrackingStatus, fetch_tracking
from guardrail_api.tools.base import RiskLevel, ToolContext
from guardrail_api.tools.order import load_order
from guardrail_api.tools.registry import register


class QueryLogisticsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: int = Field(ge=1, description="订单 ID")


class TrackingEventView(BaseModel):
    occurred_at: datetime
    status: TrackingStatus
    location: str
    description: str


class TrackingView(BaseModel):
    order_no: str
    status: TrackingStatus
    carrier: str
    tracking_no: str
    latest_update_at: datetime | None = None
    events: list[TrackingEventView]
    note: str | None = None


@register(
    name="query_logistics",
    title="查询物流轨迹",
    description=(
        "查询订单的承运商与物流轨迹。订单尚未发货时返回 status=NOT_SHIPPED 与空轨迹,"
        "这是正常业务事实而非错误。数据来自外部承运商系统,只读。"
    ),
    risk_level=RiskLevel.READ_ONLY,
    params_model=QueryLogisticsParams,
    result_model=TrackingView,
    preconditions=("订单必须存在",),
    tags=("logistics", "read", "external"),
)
async def query_logistics(context: ToolContext, params: QueryLogisticsParams) -> TrackingView:
    order = await load_order(context, params.order_id, None)
    tracking = await fetch_tracking(order.order_no, shipped_at=order.shipped_at)

    note = None
    if tracking.status is TrackingStatus.NOT_SHIPPED:
        note = f"订单 {order.order_no} 当前状态 {order.status.value},尚未发货,暂无物流轨迹"
    elif tracking.status is TrackingStatus.DELIVERED:
        note = "快件已签收"

    return TrackingView(
        order_no=tracking.order_no,
        status=tracking.status,
        carrier=tracking.carrier,
        tracking_no=tracking.tracking_no,
        latest_update_at=tracking.latest_at,
        events=[
            TrackingEventView(
                occurred_at=event.occurred_at,
                status=event.status,
                location=event.location,
                description=event.description,
            )
            for event in tracking.events
        ],
        note=note,
    )
