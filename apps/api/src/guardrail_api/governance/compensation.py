"""补偿(Saga):把已经发生的写操作,按**逆序**收回去。

为什么需要它:一个计划跑到一半失败,前半段的写已经落库了。此时只有两种选择 ——
要么把前面那几步撤掉,要么把它留成"半成品"并让人来收拾。
这个模块负责前者,并且**只在能证明撤得干净时才撤**:

1. **只有声明的才补偿。** 工具必须声明 `compensate_tool` 与 `compensate_args`,
   补偿参数只能来自原步骤的**产出**或字面量。取不到值 = 不能自动补偿 ——
   补偿写错比不补偿更糟,它会以"系统自动回滚"的名义动生产数据。
2. **逆序执行。** 后做的先撤。正向 A→B,补偿必须 B→A,否则中间会短暂出现
   "B 已经撤了但 A 的结果还在"的状态,而那个状态没有任何人验证过。
3. **撤不干净就停下来。** 只要有一步没有补偿动作、或参数取不到,
   整条补偿计划就不自动执行:run 停在 FAILED 并把「谁需要人工处理」写进失败原因。
   部分补偿比不补偿更难排查 —— 所以要么全撤,要么明确告诉人哪儿撤不了。

补偿动作本身也必须过策略引擎(见 executor):只读档的执行体连补偿都不能做,
否则"回滚"会变成一条绕过权限的路。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from guardrail_api.domain.errors import NotFound
from guardrail_api.governance.plan import lookup
from guardrail_api.tools.base import ToolSpec
from guardrail_api.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class CompletedStep:
    """一个已经成功、可能要回滚的步骤。"""

    seq: int
    tool: str
    result: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class CompensationStep:
    """一条补偿指令。seq 用**负数**:第 1 步的补偿是 -1,一眼看得出补偿了谁。"""

    seq: int
    source_seq: int
    source_tool: str
    tool: str
    args: dict[str, Any]

    @property
    def reason(self) -> str:
        return f"补偿第 {self.source_seq} 步 {self.source_tool} 的副作用:{self.tool}"


@dataclass(frozen=True, slots=True)
class CompensationPlan:
    steps: list[CompensationStep]
    #: 撤不掉的部分及原因。非空就不要自动补偿 —— 见模块开头的第 3 条。
    blockers: list[str]

    @property
    def is_executable(self) -> bool:
        return not self.blockers and bool(self.steps)


def _build_args(
    spec: ToolSpec, completed: CompletedStep
) -> tuple[dict[str, Any] | None, str | None]:
    """把补偿参数从声明变成实际入参。取不到就返回原因,不猜默认值。"""
    assert spec.compensate_args is not None
    args: dict[str, Any] = {}
    for name, binding in spec.compensate_args.items():
        if binding.result_path is not None:
            if completed.result is None:
                return None, f"第 {completed.seq} 步没有留下产出,取不到 {name}"
            try:
                args[name] = lookup(completed.result, binding.result_path.split("."))
            except Exception as exc:  # PlanError 及其子类:路径不存在
                return None, f"第 {completed.seq} 步的产出里取不到 {binding.result_path}:{exc}"
        else:
            args[name] = binding.const
    return args, None


def plan(completed: Sequence[CompletedStep], *, registry: ToolRegistry) -> CompensationPlan:
    """给已成功的步骤排一份补偿计划。**逆序**:后做的先撤。"""
    steps: list[CompensationStep] = []
    blockers: list[str] = []

    for item in sorted(completed, key=lambda step: step.seq, reverse=True):
        try:
            spec = registry.get(item.tool)
        except NotFound as exc:
            # 工具可能已经从注册表里下线(改名 / 删掉),但库里还留着它的产出。
            # 这时候必须报 blocker 停手,而不是抛异常让补偿半路死掉。
            blockers.append(f"第 {item.seq} 步 {item.tool} 无法补偿:{exc}")
            continue
        if spec.is_read_only:
            # 只读步骤没有副作用可撤,跳过是正确行为,不是"漏了"
            continue
        if not spec.compensate_tool:
            blockers.append(f"第 {item.seq} 步 {item.tool} 没有声明补偿动作,无法自动撤销")
            continue

        try:
            target = registry.get(spec.compensate_tool)
        except NotFound as exc:
            blockers.append(
                f"第 {item.seq} 步 {item.tool} 声明的补偿动作 {spec.compensate_tool} 不可用:{exc}"
            )
            continue
        args, problem = _build_args(spec, item)
        if args is None:
            blockers.append(f"第 {item.seq} 步 {item.tool} 无法补偿:{problem}")
            continue
        steps.append(
            CompensationStep(
                seq=-item.seq,
                source_seq=item.seq,
                source_tool=item.tool,
                tool=target.name,
                args=args,
            )
        )

    return CompensationPlan(steps=steps, blockers=blockers)
