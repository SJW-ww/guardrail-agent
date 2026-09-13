"""LLM 规划器(OpenAI 兼容 chat/completions)。

分工写死在这里:**模型负责"读懂人话 + 做业务判断",系统负责"解析主键 + 校验 + 定档"。**

- 主键不由模型生成:先用正则把订单捞出来,再从库里取真实订单,注入提示词;
- 工具清单来自 `registry.describe()`:新增工具自动出现在提示词里,不用改 prompt;
- 工具声明是**唯一权威**:模型填的 risk_level / requires_approval 一律丢弃,
  换成 ToolSpec 的值 —— 让模型给自己的操作定风险等级,等于让它自己发通行证;
- 输出不过关就把校验错误回喂重写,重试 `llm_max_repair_attempts` 次。

每次调用都会把模型输出原样记进日志。审计要留痕的不是"模型的自我叙述",
但排查问题时必须能看到模型当时到底说了什么。
"""

import json
import logging
from typing import Any

import httpx
from pydantic import ValidationError

from guardrail_api.config import Settings, get_settings
from guardrail_api.domain.errors import (
    PlanError,
    PlannerUnavailable,
    ProposalRejected,
    ToolArgumentError,
)
from guardrail_api.governance import plan as plan_module
from guardrail_api.models import Order
from guardrail_api.planner import prompts
from guardrail_api.planner.context import build_order_context, resolve_order
from guardrail_api.planner.finalize import finalize
from guardrail_api.planner.proposal import Plan, PlanStep
from guardrail_api.tools.registry import ToolRegistry, load_tools

logger = logging.getLogger("guardrail.planner.llm")


def _describe_validation_error(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '__root__'}: {error['msg']}"
        for error in exc.errors(include_url=False, include_input=False)
    )


class LLMPlanner:
    """一句意图 → 一份计划(一到多步)。计划里的依赖与引用由 `governance.plan` 校验。"""

    def __init__(
        self,
        settings: Settings | None = None,
        registry: ToolRegistry | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.registry = registry or load_tools()
        self._client = client

    async def draft(self, session: Any, intent: str, *, actor: str) -> Plan:
        order = await resolve_order(session, intent)
        context = await build_order_context(session, order)

        messages: list[dict[str, str]] = [
            {"role": "system", "content": prompts.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": prompts.build_user_prompt(
                    intent=intent, context=context, tools=self.registry.describe()
                ),
            },
        ]

        attempts: list[str] = []
        max_attempts = self.settings.llm_max_repair_attempts + 1
        for attempt in range(1, max_attempts + 1):
            raw = await self._complete(messages)
            try:
                plan = self._validate_plan(raw, order=order, intent=intent)
            except ProposalRejected as exc:
                attempts.append(f"第 {attempt} 次:{exc.message}")
                logger.warning("LLM 提议未通过校验(第 %d/%d 次):%s", attempt, max_attempts, exc)
                if attempt >= max_attempts:
                    break
                messages.append({"role": "assistant", "content": raw})
                repair = prompts.build_repair_prompt(exc.message)
                messages.append({"role": "user", "content": repair})
                continue

            plan.planner = "llm"
            # 定档与审批交给策略引擎,模型填的 risk_level / requires_approval 一律作废
            plan = finalize(plan, actor=actor, registry=self.registry, settings=self.settings)
            logger.info(
                "LLM 计划通过校验:steps=%s confidence=%.2f(第 %d 次尝试)",
                [step.action for step in plan.steps],
                plan.confidence,
                attempt,
            )
            return plan

        last = attempts[-1] if attempts else "未知"
        raise ProposalRejected(
            f"模型连续 {max_attempts} 次没能给出合规计划,最后一次错误:{last}",
            attempts=attempts,
            order_no=order.order_no,
        )

    # ---------- 与模型服务交互 ----------

    async def _complete(self, messages: list[dict[str, str]]) -> str:
        settings = self.settings
        payload: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": messages,
            "temperature": settings.llm_temperature,
            # 要求返回 JSON 对象。个别 OpenAI 兼容供应商不认这个参数,见下面的兜底。
            "response_format": {"type": "json_object"},
        }

        async with self._client_scope() as client:
            try:
                response = await self._post(client, payload)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 400 and "response_format" in exc.response.text:
                    logger.warning("供应商不认 response_format=json_object,去掉该参数重试")
                    payload.pop("response_format", None)
                    response = await self._post(client, payload)
                else:
                    raise
            body = response.json()

        content = self._extract_content(body)
        usage = body.get("usage") or {}
        logger.info(
            "LLM 调用完成 model=%s prompt_tokens=%s completion_tokens=%s",
            body.get("model", settings.llm_model),
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
        )
        return content

    async def _post(self, client: httpx.AsyncClient, payload: dict[str, Any]) -> httpx.Response:
        url = f"{self.settings.llm_base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        try:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 401 / 403 / 404 基本都是配置问题,直接暴露状态码,别让人猜
            raise PlannerUnavailable(
                f"模型服务返回 {exc.response.status_code}:{exc.response.text[:200]}",
                base_url=self.settings.llm_base_url,
            ) from exc
        except httpx.HTTPError as exc:
            raise PlannerUnavailable(
                f"模型服务不可达:{type(exc).__name__}", base_url=self.settings.llm_base_url
            ) from exc
        return response

    def _client_scope(self) -> Any:
        """复用注入的 client(测试用 MockTransport),否则每次调用新建一个。

        新建时必须显式超时 —— 一个卡住的模型调用会一直占着这个 run 的租约。
        """
        if self._client is not None:
            return _NullContext(self._client)
        return httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds)

    @staticmethod
    def _extract_content(body: dict[str, Any]) -> str:
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise PlannerUnavailable(f"模型响应结构异常:{json.dumps(body)[:200]}") from exc
        if not isinstance(content, str) or not content.strip():
            raise PlannerUnavailable("模型返回了空内容")
        return content

    # ---------- 校验:把模型的输出挡在执行器之外 ----------

    def _validate_plan(self, raw: str, *, order: Order, intent: str) -> Plan:
        """把模型输出校验成一份**可以交给执行器**的计划。

        模型负责"读懂人话 + 做业务判断",系统负责"钉死主键 + 校验结构"。
        任何一步不合格,整份计划退回重写 —— 让模型"先跑两步试试"不是规划,是撞运气。
        """
        try:
            data = prompts.extract_json(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProposalRejected(f"输出不是合法 JSON:{exc}") from exc

        if not isinstance(data, dict):
            raise ProposalRejected(f"输出必须是 JSON 对象,实际是 {type(data).__name__}")

        # goal 缺失不算硬错误:它就是一句话描述,系统能补,补出来的还更准确。
        if not str(data.get("goal") or "").strip():
            data["goal"] = f"针对订单 {order.order_no}:{intent}"

        try:
            plan = Plan.model_validate(data)
        except ValidationError as exc:
            raise ProposalRejected(f"计划字段不合规:{_describe_validation_error(exc)}") from exc

        for step in plan.steps:
            self._validate_step(step, order=order)

        # 结构校验(seq 递增、依赖只能回头、引用只能指向更早的步骤)和执行器用的是同一份。
        try:
            plan_module.validate(plan.steps)
        except PlanError as exc:
            raise ProposalRejected(f"计划的步骤结构不合法:{exc.message}") from exc

        return plan

    def _validate_step(self, step: PlanStep, *, order: Order) -> None:
        if step.action not in self.registry:
            raise ProposalRejected(f"工具 {step.action} 不存在,可用工具:{self.registry.names()}")
        spec = self.registry.get(step.action)

        # 资源级校验:这次操作实际落在哪个订单上,由系统解析的结果说了算。
        # 参考 Google AIP-211 —— 服务端必须自己校验资源级权限,
        # 不能相信调用方(这里是模型)传进来的资源标识。
        arguments = step.arguments
        target_id = arguments.get("order_id")
        if target_id is not None and not plan_module.is_ref(target_id):
            try:
                mismatched = int(target_id) != order.id
            except (TypeError, ValueError):
                mismatched = True
            if mismatched:
                raise ProposalRejected(
                    f"第 {step.seq} 步的 order_id={target_id} "
                    f"与已解析的订单 {order.id}({order.order_no})不一致"
                )
        target_no = arguments.get("order_no")
        if target_no is not None and not plan_module.is_ref(target_no):
            if str(target_no) != order.order_no:
                raise ProposalRejected(
                    f"第 {step.seq} 步的 order_no={target_no} "
                    f"与已解析的订单 {order.order_no} 不一致"
                )
            # 订单号对得上,但工具只收 order_id(如 create_refund)就换成系统解析的主键,
            # 避免"写对了内容、却因为字段名被判不合规"的无谓重试
            if "order_id" not in (spec.params_model.model_fields or {}):
                arguments.pop("order_no")
        arguments.setdefault("order_id", order.id)

        # 含引用的参数要等上游跑完才能校验,这里先跳过 ——
        # 不是少校验一次,而是把这次校验挪到解析之后(工具的入口一个都不少)。
        if next(plan_module.iter_refs(arguments), None) is not None:
            return
        try:
            self.registry.validate_args(step.action, arguments)
        except ToolArgumentError as exc:
            raise ProposalRejected(f"第 {step.seq} 步入参不合规:{exc.message}") from exc


class _NullContext:
    """给注入的 client 用:不拥有它,所以不关闭它。"""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, *_: object) -> None:
        return None
