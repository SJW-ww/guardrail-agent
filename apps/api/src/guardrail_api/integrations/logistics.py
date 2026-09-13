"""承运商物流查询(离线模拟)。

真实系统这里会调承运商 API。做成确定性模拟有两个好处:
1. 演示与评测完全离线,不受外部额度/网络影响;
2. 同一订单号永远得到同一条轨迹,回归测试才有可比性。

注意:这是**只读的外部系统**,不碰本地库 —— 工具层需要能包装非数据库数据源。
"""

import hashlib
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

CARRIERS = ("顺丰速运", "京东物流", "中通快递")


class TrackingStatus(StrEnum):
    NOT_SHIPPED = "NOT_SHIPPED"
    PICKED_UP = "PICKED_UP"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"


@dataclass(frozen=True, slots=True)
class TrackingEvent:
    occurred_at: datetime
    status: TrackingStatus
    location: str
    description: str


@dataclass(frozen=True, slots=True)
class TrackingInfo:
    order_no: str
    tracking_no: str
    carrier: str
    status: TrackingStatus
    events: list[TrackingEvent] = field(default_factory=list)

    @property
    def latest_at(self) -> datetime | None:
        return self.events[-1].occurred_at if self.events else None


def _seed_for(order_no: str) -> int:
    return int.from_bytes(hashlib.sha256(order_no.encode()).digest()[:4], "big")


async def fetch_tracking(order_no: str, *, shipped_at: datetime | None) -> TrackingInfo:
    rng = random.Random(_seed_for(order_no))
    carrier = rng.choice(CARRIERS)
    tracking_no = f"SF{rng.randint(10**11, 10**12 - 1)}"

    if shipped_at is None:
        # 未发货是正常的业务事实,不是异常 —— 用结构化结果表达,让 Agent 自己判断
        return TrackingInfo(
            order_no=order_no, tracking_no="", carrier="", status=TrackingStatus.NOT_SHIPPED
        )

    route = rng.sample(
        ("深圳转运中心", "东莞分拨中心", "广州集散中心", "上海分拨中心", "北京转运中心"), 3
    )
    events = [
        TrackingEvent(
            occurred_at=shipped_at + timedelta(hours=2),
            status=TrackingStatus.PICKED_UP,
            location=route[0],
            description="快件已揽收",
        ),
        TrackingEvent(
            occurred_at=shipped_at + timedelta(hours=14),
            status=TrackingStatus.IN_TRANSIT,
            location=route[1],
            description="快件已发往下一站",
        ),
    ]

    delivered = rng.random() < 0.6
    if delivered:
        events.append(
            TrackingEvent(
                occurred_at=shipped_at + timedelta(hours=32),
                status=TrackingStatus.DELIVERED,
                location=route[2],
                description="快件已签收",
            )
        )

    return TrackingInfo(
        order_no=order_no,
        tracking_no=tracking_no,
        carrier=carrier,
        status=events[-1].status,
        events=events,
    )
