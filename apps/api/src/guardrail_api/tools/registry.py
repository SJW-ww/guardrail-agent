"""工具注册表。

模块级自动发现:往 `guardrail_api/tools/` 里丢一个新模块,它就自动出现在注册表里,
编排层不用改一行代码。注册期做声明完整性校验 —— 声明不合规的写工具**根本注册不上**。
"""

import importlib
import pkgutil
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from guardrail_api.domain.errors import NotFound, ToolArgumentError
from guardrail_api.tools.base import (
    RiskLevel,
    ToolContext,
    ToolHandler,
    ToolSnapshot,
    ToolSpec,
)

_SKIP_MODULES = frozenset({"base", "registry"})


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        self._assert_declarable(spec)
        self._specs[spec.name] = spec
        return spec

    def _assert_declarable(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise RuntimeError(f"工具名重复:{spec.name}")
        if not spec.name.isidentifier():
            raise RuntimeError(f"工具名必须是合法标识符:{spec.name}")

        if spec.is_read_only:
            if spec.side_effect is not None:
                raise RuntimeError(f"只读工具 {spec.name} 不应声明副作用")
            return

        # 以下三条是硬性要求,不是建议:声明缺失就不允许接入
        if not spec.side_effect:
            raise RuntimeError(f"写工具 {spec.name} 必须声明 side_effect")
        if spec.snapshot is None:
            raise RuntimeError(
                f"写工具 {spec.name} 必须声明审计快照 snapshot —— "
                "没有 before/after 的写操作在审计上等于没发生过"
            )
        if spec.risk_level is RiskLevel.HIGH:
            if not spec.idempotent or not spec.idempotency_key:
                raise RuntimeError(
                    f"高风险工具 {spec.name} 必须声明幂等策略(idempotent + idempotency_key)"
                )
            if not spec.compensate_tool:
                raise RuntimeError(f"高风险工具 {spec.name} 必须声明补偿动作 compensate_tool")

    def validate(self) -> None:
        """加载后自检:补偿动作必须真实存在,否则「可回滚」就是空话。"""
        for spec in self._specs.values():
            if spec.compensate_tool and spec.compensate_tool not in self._specs:
                raise RuntimeError(f"工具 {spec.name} 声明的补偿动作 {spec.compensate_tool} 未注册")

    def get(self, name: str) -> ToolSpec:
        spec = self._specs.get(name)
        if spec is None:
            raise NotFound(f"工具 {name} 不存在,可用工具:{sorted(self._specs)}")
        return spec

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __len__(self) -> int:
        return len(self._specs)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def all(self) -> list[ToolSpec]:
        return [self._specs[name] for name in self.names()]

    def describe(self) -> list[dict[str, Any]]:
        return [spec.describe() for spec in self.all()]

    async def invoke(
        self,
        name: str,
        context: ToolContext,
        arguments: Mapping[str, Any],
        *,
        params: BaseModel | None = None,
    ) -> BaseModel:
        """唯一的调用入口。

        W2 的幂等落表、W4 的策略裁决、审计写入都挂在这一个点上 ——
        只要所有调用都走这里,就没有绕过的可能。

        `params` 允许执行器传入已经校验过的参数(它需要用同一个对象取审计快照),
        避免同一份入参被校验两次。
        """
        spec = self.get(name)
        validated = params if params is not None else self.validate_args(name, arguments)
        return await spec.handler(context, validated)

    def validate_args(self, name: str, arguments: Mapping[str, Any]) -> BaseModel:
        """把原始入参校验成模型。校验失败会抛出带字段级错误的 ToolArgumentError。"""
        spec = self.get(name)
        return self._validate(spec, arguments)

    @staticmethod
    def _validate(spec: ToolSpec, arguments: Mapping[str, Any]) -> BaseModel:
        try:
            return spec.params_model.model_validate(dict(arguments))
        except ValidationError as exc:
            errors = [
                {
                    "field": ".".join(str(part) for part in error["loc"]) or "__root__",
                    "message": error["msg"],
                    "type": error["type"],
                }
                for error in exc.errors(include_url=False, include_input=False)
            ]
            detail = "; ".join(f"{item['field']}: {item['message']}" for item in errors)
            raise ToolArgumentError(
                f"工具 {spec.name} 参数校验失败:{detail}",
                tool=spec.name,
                errors=errors,
            ) from exc


registry = ToolRegistry()


def register(
    *,
    name: str,
    title: str,
    description: str,
    risk_level: RiskLevel,
    params_model: type[BaseModel],
    result_model: type[BaseModel] | None = None,
    preconditions: tuple[str, ...] = (),
    side_effect: str | None = None,
    idempotent: bool = False,
    idempotency_key: str | None = None,
    compensate_tool: str | None = None,
    snapshot: ToolSnapshot | None = None,
    reason_field: str | None = None,
    tags: tuple[str, ...] = (),
) -> Any:
    """把一个 async 函数注册成领域工具。"""

    def decorator(handler: ToolHandler) -> ToolHandler:
        registry.register(
            ToolSpec(
                name=name,
                title=title,
                description=description,
                risk_level=risk_level,
                params_model=params_model,
                result_model=result_model,
                handler=handler,
                preconditions=preconditions,
                side_effect=side_effect,
                idempotent=idempotent,
                idempotency_key=idempotency_key,
                compensate_tool=compensate_tool,
                snapshot=snapshot,
                reason_field=reason_field,
                tags=tags,
            )
        )
        return handler

    return decorator


_loaded = False


def load_tools() -> ToolRegistry:
    """自动发现本包下所有工具模块并完成自检(重复调用是安全的)。"""
    global _loaded
    if _loaded:
        return registry

    package = importlib.import_module("guardrail_api.tools")
    for module_info in pkgutil.iter_modules(package.__path__):
        if module_info.name.startswith("_") or module_info.name in _SKIP_MODULES:
            continue
        importlib.import_module(f"{package.__name__}.{module_info.name}")

    registry.validate()
    _loaded = True
    return registry
