"""演示数据:100 客户 / 200 商品 / 500 订单 / 200 售后工单。

可重复执行:已有数据默认跳过,`--reset` 清空重建。
随机数用固定种子,所以每次重建出来的数据是一致的 —— 评测和录屏才有可比性。

用法:
    uv run python -m guardrail_api.scripts.seed
    uv run python -m guardrail_api.scripts.seed --reset
"""

import argparse
import asyncio
import random

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.auth import hash_password
from guardrail_api.config import get_settings
from guardrail_api.db import dispose_engine, get_session_factory
from guardrail_api.models import (
    REFUND_REASON_CODES,
    AftersalesTicket,
    Customer,
    CustomerTier,
    Inventory,
    Order,
    OrderItem,
    OrderStatus,
    Principal,
    Product,
    ProductStatus,
)
from guardrail_api.services import aftersales
from guardrail_api.services import order as order_service
from guardrail_api.services.order import Address, OrderLine

SEED = 20260101
CUSTOMER_COUNT = 100
PRODUCT_COUNT = 200
ORDER_COUNT = 500
TICKET_COUNT = 200
INITIAL_STOCK = 300

# 演示账号:`<身份> = <角色>`。角色只在双人复核里起作用(两个不同角色才算两个人),
# 但它是组织事实,不该由调用方在请求头里自称。
DEMO_ACCOUNTS = (
    ("supervisor-01", "林主管", "supervisor"),
    ("supervisor-02", "陈主管", "supervisor"),
    ("finance-01", "周财务", "finance"),
    ("operator-01", "客服小吴", "operations"),
)

WAREHOUSES = ("WH-SZ-01", "WH-DG-02", "WH-SH-03")
CITIES = ("深圳市南山区科技园南路 1 号", "东莞市松山湖园区 8 栋", "上海市浦东新区张江路 66 号")
CATEGORIES = ("服务器配件", "存储设备", "网络设备", "电源模块", "线缆耗材")

# 订单最终状态分布,加起来是 100
ORDER_DISTRIBUTION = (
    (OrderStatus.COMPLETED, 40),
    (OrderStatus.SHIPPED, 25),
    (OrderStatus.PAID, 20),
    (OrderStatus.CREATED, 15),
)


async def _reset(session: AsyncSession) -> None:
    # 按外键依赖倒序删,避免触发 RESTRICT
    # 注意:这里**不删** audit_log / agent_run。审计表被数据库触发器保护,
    # 删不掉是设计使然 —— 演示数据可以重置,历史不行。
    for model in (AftersalesTicket, OrderItem, Order, Inventory, Product, Customer):
        await session.execute(delete(model))
    await session.flush()


async def _seed_after_sales(session: AsyncSession, rng: random.Random) -> int:
    refundable_orders = list(
        await session.scalars(
            select(Order)
            .where(Order.status.in_([OrderStatus.PAID, OrderStatus.SHIPPED, OrderStatus.COMPLETED]))
            .order_by(Order.id)
        )
    )
    if not refundable_orders:
        return 0

    created = 0
    for index in range(TICKET_COUNT):
        order = refundable_orders[index % len(refundable_orders)]
        reason_code = rng.choice(sorted(REFUND_REASON_CODES))
        try:
            ticket = await aftersales.request_refund(
                session,
                order_id=order.id,
                reason_code=reason_code,
                description=f"演示工单:客户反馈 {reason_code.lower()}",
            )
        except Exception:
            # 该订单可退额度已被前面的工单占满,跳过即可
            continue

        roll = rng.random()
        if roll < 0.60:
            pass  # 留在 PENDING,给审批中心做数据
        elif roll < 0.75:
            await aftersales.approve_ticket(session, ticket.id, operator="supervisor-01")
        elif roll < 0.95:
            await aftersales.approve_ticket(session, ticket.id, operator="supervisor-01")
            await aftersales.execute_refund(session, ticket.id, operator="finance-01")
        else:
            await aftersales.reject_ticket(
                session, ticket.id, operator="supervisor-02", reason="不符合退款条件"
            )
        created += 1

    return created


async def seed(session: AsyncSession) -> dict[str, int]:
    rng = random.Random(SEED)

    customers = [
        Customer(
            name=f"客户{index:03d}",
            email=f"customer{index:03d}@example.com",
            phone=f"138{rng.randint(10_000_000, 99_999_999)}",
            tier=CustomerTier.VIP if index % 10 == 0 else CustomerTier.NORMAL,
        )
        for index in range(1, CUSTOMER_COUNT + 1)
    ]
    session.add_all(customers)
    await session.flush()

    products = [
        Product(
            sku=f"SKU-{index:04d}",
            name=f"{rng.choice(CATEGORIES)}-{index:04d}",
            category=rng.choice(CATEGORIES),
            price_cents=rng.choice((1990, 4990, 12900, 25900, 89900, 199000)),
            status=ProductStatus.ON_SALE if index % 20 else ProductStatus.OFF_SHELF,
        )
        for index in range(1, PRODUCT_COUNT + 1)
    ]
    session.add_all(products)
    await session.flush()

    session.add_all(
        Inventory(
            product_id=product.id,
            warehouse_code=rng.choice(WAREHOUSES),
            available_qty=INITIAL_STOCK,
            reserved_qty=0,
        )
        for product in products
    )
    await session.flush()

    on_sale_products = [product for product in products if product.status is ProductStatus.ON_SALE]
    statuses: list[OrderStatus] = []
    for status, weight in ORDER_DISTRIBUTION:
        statuses.extend([status] * (ORDER_COUNT * weight // 100))
    while len(statuses) < ORDER_COUNT:
        statuses.append(OrderStatus.CREATED)
    rng.shuffle(statuses)

    for index, target_status in enumerate(statuses, start=1):
        customer = rng.choice(customers)
        lines = [
            OrderLine(product_id=product.id, quantity=rng.randint(1, 3))
            for product in rng.sample(on_sale_products, rng.randint(1, 3))
        ]
        order = await order_service.create_order(
            session,
            customer_id=customer.id,
            lines=lines,
            address=Address(
                receiver_name=customer.name,
                receiver_phone=customer.phone,
                address=rng.choice(CITIES),
            ),
            order_no=f"SO2026{index:06d}",
        )

        if target_status is OrderStatus.CREATED:
            continue
        await order_service.pay_order(session, order.id)
        if target_status is OrderStatus.PAID:
            continue
        await order_service.ship_order(session, order.id)
        if target_status is OrderStatus.SHIPPED:
            continue
        await order_service.complete_order(session, order.id)

    ticket_count = await _seed_after_sales(session, rng)
    await session.commit()

    return {
        "customers": len(customers),
        "products": len(products),
        "orders": ORDER_COUNT,
        "tickets": ticket_count,
    }


async def _existing_orders(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(Order)) or 0)


async def ensure_principals(session: AsyncSession) -> int:
    """幂等地建好演示账号。

    已存在的账号**不动**:重灌演示数据不该把别人改过的口令重置回去。
    """
    settings = get_settings()
    created = 0
    for username, display_name, role in DEMO_ACCOUNTS:
        exists = await session.scalar(select(Principal).where(Principal.username == username))
        if exists is not None:
            continue
        session.add(
            Principal(
                username=username,
                display_name=display_name,
                role=role,
                password_hash=hash_password(settings.demo_password),
            )
        )
        created += 1
    await session.commit()
    return created


async def main(reset: bool) -> None:
    factory = get_session_factory()
    async with factory() as session:
        new_accounts = await ensure_principals(session)
        if new_accounts:
            print(f"身份:{new_accounts} 个演示账号已建好(口令见 DEMO_PASSWORD,默认 guardrail-demo)")

        existing = await _existing_orders(session)
        if existing and not reset:
            print(f"已有 {existing} 条订单,跳过。需要重建请加 --reset")
            return
        if reset:
            print("--reset:清空业务数据 ...")
            await _reset(session)

        counts = await seed(session)
        print(
            "seed 完成:"
            f" {counts['customers']} 客户 /"
            f" {counts['products']} 商品 /"
            f" {counts['orders']} 订单 /"
            f" {counts['tickets']} 售后工单"
        )


async def _run(reset: bool) -> None:
    # 必须在同一个事件循环里建引擎与释放引擎,否则连接池还挂在已关闭的 loop 上
    try:
        await main(reset)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="灌入 GuardRail 演示数据")
    parser.add_argument("--reset", action="store_true", help="先清空业务数据再灌")
    args = parser.parse_args()

    asyncio.run(_run(args.reset))
