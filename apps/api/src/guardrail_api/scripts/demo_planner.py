"""LLM 规划器演示:让真模型读一句人话,产出结构化提议,再由系统裁决。

    make demo-planner                            # 跑固定的几条意图
    make demo-planner i="把 SO2026000005 退了"   # 临时再加一条

需要先在 `.env` 里配好 `PLANNER_BACKEND=llm` / `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`。
**会消耗 token**,所以不进 CI —— CI 走 deterministic 规划器,离线且免费。

它演示的是「模型只提议、系统做决策」这条边界落在哪:

- 订单主键由系统从库里取,模型编一个不存在的订单号会被打回重写;
- 模型在提议里填的 risk_level / requires_approval 一律作废,换成工具声明的值;
- 每一步的裁决(ALLOW / REQUIRE_APPROVAL / DENY)与理由由策略引擎给出,不经过模型。

脚本只调 `planner.draft`,**不登记 run、不产生任何副作用**;跑完库里一行都不多。
"""

import argparse
import asyncio
import json
import logging
import os
import re
import time
from typing import Any

from sqlalchemy import select

from guardrail_api.config import get_settings
from guardrail_api.db import dispose_engine, get_session_factory
from guardrail_api.domain.errors import DomainError
from guardrail_api.models import Order, OrderStatus
from guardrail_api.planner import draft, planner_backend

#: 演示用的执行体。默认信任等级由 POLICY_DEFAULT_TRUST_LEVEL 决定(L2):
#: 只读自动放行、高风险写停下来等人批 —— 这正是要演示的那条边界。
DEMO_ACTOR = "human:demo-operator"

PLANNER_LOGGER = "guardrail.planner.llm"
TOKEN_RE = re.compile(r"prompt_tokens=(\S+) completion_tokens=(\S+)")


def title(text: str) -> None:
    print(f"\n\033[36m{'=' * 4} {text}\033[0m")


class Recorder(logging.Handler):
    """把规划器自己打的日志收下来,用来统计"调了几次模型、修了几次"。

    不解析返回值 —— LLMPlanner 不吐 usage,日志是它唯一的出处。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.repairs: list[str] = []
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if "未通过校验" in message:
            self.repairs.append(message)
        elif "调用完成" in message:
            self.calls += 1
            matched = TOKEN_RE.search(message)
            if matched:
                for attr, value in zip(
                    ("prompt_tokens", "completion_tokens"), matched.groups(), strict=True
                ):
                    if value.isdigit():
                        setattr(self, attr, getattr(self, attr) + int(value))

    def reset(self) -> None:
        self.repairs.clear()
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0


def collect_refs(value: Any) -> list[str]:
    """把参数里的 `{"$ref": "1.available_refund_cents"}` 挖出来,好一眼看出多步引用。"""
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return [str(value["$ref"])]
        return [ref for item in value.values() for ref in collect_refs(item)]
    if isinstance(value, list):
        return [ref for item in value for ref in collect_refs(item)]
    return []


async def pick_orders(factory, limit: int = 2) -> list[Order]:
    """挑几张真实订单当输入。规划器不执行,所以只要求订单存在、状态合理。"""

    async def query(status: OrderStatus) -> list[Order]:
        async with factory() as session:
            return list(
                (
                    await session.execute(
                        select(Order).where(Order.status == status).order_by(Order.id).limit(limit)
                    )
                )
                .scalars()
                .all()
            )

    picked = [*await query(OrderStatus.PAID), *await query(OrderStatus.SHIPPED)]
    if not picked:
        raise SystemExit("演示数据为空 —— 请先执行 `make seed-reset`")
    return picked


def build_cases(orders: list[Order]) -> list[dict[str, str]]:
    """固定几条意图:单步 / 多步引用 / 只读 / 部分退款,外加一条调模型前就该被拦的。"""
    paid = [order for order in orders if order.status is OrderStatus.PAID] or orders
    shipped = [order for order in orders if order.status is OrderStatus.SHIPPED] or orders
    return [
        {
            "name": "口语化·单步",
            "intent": f"帮我把 {paid[0].order_no} 这单退了吧",
            "expect": "plan",
        },
        {
            # 单步还是两步由模型自己决定 —— 演示不替它预设,只把结果如实打出来。
            "name": "退款·金额按额度",
            "intent": f"{paid[-1].order_no} 要退款,金额按可退额度来",
            "expect": "plan",
        },
        {"name": "只读", "intent": f"查一下 {shipped[0].order_no} 的物流到哪了", "expect": "plan"},
        {"name": "部分退款", "intent": f"给 {shipped[-1].order_no} 退 50 块钱", "expect": "plan"},
        {
            "name": "显式两步·参数引用上一步产出",
            "intent": f"先把 {shipped[0].order_no} 的可退额度查出来,再按这个额度退款",
            "expect": "plan",
        },
        {
            "name": "无订单号(调模型前就该拦)",
            "intent": "帮我退款",
            "expect": "rejected_without_model",
        },
    ]


async def run_case(
    planner_actor: str, case: dict[str, str], recorder: Recorder
) -> tuple[bool, list[str]]:
    factory = get_session_factory()
    recorder.reset()
    started = time.monotonic()
    plan = None
    error: Exception | None = None
    try:
        async with factory() as session:
            plan = await draft(session, case["intent"], actor=planner_actor)
    except DomainError as exc:
        error = exc
    elapsed = time.monotonic() - started

    expect = case["expect"]
    ok = (plan is not None) if expect == "plan" else (plan is None and recorder.calls == 0)

    print(f"\n  {'✓' if ok else '✗'} {case['name']}  「{case['intent']}」")
    print(
        f"    模型调用={recorder.calls} 次 · 修复重写={len(recorder.repairs)} 次 · "
        f"耗时={elapsed:.1f}s · tokens={recorder.prompt_tokens}/{recorder.completion_tokens}"
    )
    for repair in recorder.repairs:
        print(f"    打回重写:{repair[:150]}")

    if plan is None:
        print(f"    未生成计划:{type(error).__name__}: {error}" if error else "    未生成计划")
        if expect == "plan":
            print("    ↑ 期望它能生成一份计划,这条算失败")
        return ok, []

    refs: list[str] = []
    print(f"    goal={plan.goal}")
    for step in plan.steps:
        args = json.dumps(step.arguments, ensure_ascii=False)
        print(f"    #{step.seq} {step.action} {args}")
        if step.depends_on:
            print(f"        依赖={step.depends_on}")
        step_refs = collect_refs(step.arguments)
        refs.extend(step_refs)
        if step_refs:
            print(f"        引用={step_refs} ← 取值由编排层在裁决前解析,不是模型填的数字")
        print(f"        裁决={step.policy_decision} —— {step.policy_reason}")
        if step.risk_level and step.risk_level != "read_only":
            print(f"        风险级={step.risk_level}(工具声明,模型说了不算)")
    return ok, refs


async def main() -> None:
    parser = argparse.ArgumentParser(description="用真模型跑一遍 LLM 规划器")
    parser.add_argument("-i", "--intent", help="额外跑一条自定义意图")
    parser.add_argument("--actor", default=DEMO_ACTOR, help=f"执行体(默认 {DEMO_ACTOR})")
    args = parser.parse_args()

    settings = get_settings()
    title("0. 检查规划器配置")
    if planner_backend() != "llm":
        raise SystemExit(
            "当前规划链路是 deterministic —— 演示会变成跑规则解析器,没有意义。\n"
            "请在 .env 里配好:PLANNER_BACKEND=llm / LLM_BASE_URL / LLM_API_KEY / LLM_MODEL"
        )
    print(f"  backend=llm base_url={settings.llm_base_url}")
    print(f"  model={settings.llm_model} timeout={settings.llm_timeout_seconds}s")
    print(f"  修复重写上限={settings.llm_max_repair_attempts} 次;执行体={args.actor}")

    factory = get_session_factory()
    orders = await pick_orders(factory)
    cases = build_cases(orders)
    if args.intent:
        cases.append({"name": "自定义", "intent": args.intent, "expect": "plan"})

    recorder = Recorder()
    logger = logging.getLogger(PLANNER_LOGGER)
    # 脚本是独立进程,没人配 logging;不显式抬到 INFO 的话规划器的日志根本不会流到 handler,
    # 「调了几次模型、修了几次」就统计不出来。
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(recorder)

    results: list[tuple[str, bool, list[str]]] = []
    try:
        title("1. 一句人话 → 一份带裁决的计划(不登记 run、无副作用)")
        for case in cases:
            ok, refs = await run_case(args.actor, case, recorder)
            results.append((case["name"], ok, refs))
    finally:
        logger.removeHandler(recorder)
        logger.setLevel(previous_level)
        await dispose_engine()

    title("2. 汇总")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, _ in results:
        print(f"  {'✓' if ok else '✗'} {name}")
    refs_total = sum(len(refs) for _, _, refs in results)
    print(f"\n  通过 {passed}/{len(results)};本次模型自主产出参数引用 {refs_total} 处")
    print("  注:一步还是多步由模型决定,因此步数不写死断言;多步引用的正确性由集成测试覆盖。")
    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    if os.environ.get("APP_ENV") == "test":
        raise SystemExit("演示脚本不要跑在测试库上")
    asyncio.run(main())
