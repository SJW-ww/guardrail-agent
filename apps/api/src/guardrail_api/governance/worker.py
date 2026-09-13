"""拉取式执行进程。

和「API 进程内起个后台任务」的区别:worker 是可独立部署、可被 kill、可以起多个副本的。
本文件存在的意义就是让「多副本抢同一个队列」「副本被 kill 后任务被别人接手」
这两件事可以被真的跑一遍,而不是嘴上说说。

    python -m guardrail_api.governance.worker                    # 常驻轮询
    python -m guardrail_api.governance.worker --once             # 领一个就跑
    python -m guardrail_api.governance.worker --run <uid> --once  # 只推进指定 run

两个混沌开关只用于演示与自动化测试,生产永远不传:

    --crash-before-step N   第 N 步事务开始前 SIGKILL(模拟进程在步骤之间被杀)
    --crash-during-step N   第 N 步已经写完业务数据、提交之前 SIGKILL(模拟提交前崩溃)
"""

import argparse
import asyncio
import logging
import os
import signal
import uuid

from guardrail_api.config import Settings, get_settings
from guardrail_api.db import dispose_engine, get_session_factory
from guardrail_api.governance import lease
from guardrail_api.governance.executor import (
    CHAOS_AFTER_COMPENSATION_COMMIT,
    CHAOS_AFTER_TOOL_WRITE,
    CHAOS_BEFORE_STEP_TXN,
    ChaosHook,
    RunExecutor,
)

logger = logging.getLogger("guardrail.worker")


def build_chaos(
    *,
    crash_before_step: int | None = None,
    crash_during_step: int | None = None,
    crash_after_compensation: int | None = None,
) -> ChaosHook | None:
    """把命令行开关翻译成执行器能用的钩子。"""
    if crash_before_step is None and crash_during_step is None and crash_after_compensation is None:
        return None

    def hook(event: str, seq: int) -> None:
        if event == CHAOS_BEFORE_STEP_TXN and seq == crash_before_step:
            _die(f"第 {seq} 步的事务还没开始,进程被 SIGKILL(已完成的前几步保持提交)")
        if event == CHAOS_AFTER_TOOL_WRITE and seq == crash_during_step:
            _die(f"第 {seq} 步业务数据已写、事务尚未提交,进程被 SIGKILL(应整体回滚)")
        if event == CHAOS_AFTER_COMPENSATION_COMMIT and seq == crash_after_compensation:
            _die(
                f"第 {seq} 步的补偿已提交、整轮补偿还没收尾,进程被 SIGKILL"
                "(run 不得被普通 worker 领走;重新补偿不得撤第二次)"
            )

    return hook


def _die(reason: str) -> None:
    logging.getLogger("guardrail.chaos").warning("CHAOS %s", reason)
    os.kill(os.getpid(), signal.SIGKILL)


async def run_worker(
    *,
    worker_id: str,
    once: bool = False,
    run_uid: str | None = None,
    poll_interval: float = 1.0,
    crash_before_step: int | None = None,
    crash_during_step: int | None = None,
    crash_after_compensation: int | None = None,
    compensate: bool = False,
    settings: Settings | None = None,
) -> int:
    settings = settings or get_settings()
    factory = get_session_factory()
    executor = RunExecutor(
        factory,
        worker_id=worker_id,
        chaos=build_chaos(
            crash_before_step=crash_before_step,
            crash_during_step=crash_during_step,
            crash_after_compensation=crash_after_compensation,
        ),
        settings=settings,
    )

    if compensate:
        # 补偿走一条独立的进程入口:它要动的是一个**已经失败**的 run,
        # 和"领一个待推进的任务"是两件事(见 lease.claim_runs 的 statuses)。
        if run_uid is None:
            raise ValueError("--compensate 需要同时给 --run <run_uid>")
        outcome = await executor.compensate(
            run_uid, actor=await executor.actor_of(run_uid), force=True
        )
        logger.info(
            "run=%s 补偿 status=%s 撤销 %d 步,blockers=%s",
            run_uid,
            outcome.status.value,
            len(outcome.compensated),
            outcome.blockers or "无",
        )
        return 1

    processed = 0
    while True:
        claimed = await _claim(
            worker_id=worker_id,
            run_uid=run_uid,
            lease_seconds=settings.lease_seconds,
            factory=factory,
        )
        if not claimed:
            if once:
                return processed
            await asyncio.sleep(poll_interval)
            continue

        for uid in claimed:
            try:
                result = await executor.execute_run(uid)
                logger.info(
                    "run=%s status=%s 本次执行 %d 步(重放 %d 步),resumed_from=%s",
                    uid,
                    result.status.value,
                    len(result.outcomes),
                    len(result.replayed),
                    result.resumed_from,
                )
            except Exception:
                logger.exception("run=%s 执行中断,等待租约到期后被重新领取", uid)
            processed += 1

        if once:
            return processed


async def _claim(
    *,
    worker_id: str,
    run_uid: str | None,
    lease_seconds: int,
    factory,
) -> list[str]:
    async with factory() as session:
        runs = await lease.claim_runs(
            session, worker_id=worker_id, lease_seconds=lease_seconds, run_uid=run_uid
        )
        uids = [run.run_uid for run in runs]
        await session.commit()
        return uids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GuardRail 执行 worker")
    parser.add_argument("--worker-id", default=f"worker-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--once", action="store_true", help="领一个 run 执行完就退出")
    parser.add_argument("--run", dest="run_uid", default=None, help="只推进指定的 run_uid")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--crash-before-step", type=int, default=None)
    parser.add_argument("--crash-during-step", type=int, default=None)
    parser.add_argument(
        "--crash-after-compensation",
        type=int,
        default=None,
        help="第 N 步的补偿已提交、整轮补偿还没收尾时 SIGKILL",
    )
    parser.add_argument(
        "--compensate",
        action="store_true",
        help="不推进计划,改为对 --run 指定的已失败 run 执行补偿",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    )

    async def runner() -> int:
        try:
            return await run_worker(
                worker_id=args.worker_id,
                once=args.once,
                run_uid=args.run_uid,
                poll_interval=args.poll_interval,
                crash_before_step=args.crash_before_step,
                crash_during_step=args.crash_during_step,
                crash_after_compensation=args.crash_after_compensation,
                compensate=args.compensate,
            )
        finally:
            await dispose_engine()

    # 退出码只表达「这一轮有没有出错」。
    # 把处理条数当退出码会让一次正常退出变成非 0 —— k8s / systemd 会当成崩溃反复重启。
    asyncio.run(runner())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
