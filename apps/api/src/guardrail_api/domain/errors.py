"""领域错误。

每个错误都带一个稳定的 `code`(给 API / 前端用)和一句**可读的中文理由**
(给审批人与审计看)—— 这是本项目「决策必须可解释」原则在代码里的落点。
"""

from typing import Any


class DomainError(Exception):
    code = "domain_error"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def __str__(self) -> str:
        return self.message


class NotFound(DomainError):
    code = "not_found"


class InvalidStateTransition(DomainError):
    code = "invalid_state_transition"


class InsufficientStock(DomainError):
    code = "insufficient_stock"


class RuleViolation(DomainError):
    code = "rule_violation"


class ToolArgumentError(DomainError):
    """工具入参校验失败。context["errors"] 是字段级错误,直接给前端标红用。"""

    code = "invalid_tool_arguments"


class LeaseLost(DomainError):
    """执行期间租约失效。持有者必须立刻停手,不能再写任何东西。"""

    code = "lease_lost"


class RunBudgetExceeded(DomainError):
    """超出步骤/Token 预算。这不是失败,是拒绝继续 —— 剩下的需要人来判断。"""

    code = "run_budget_exceeded"


class PlanError(RuleViolation):
    """计划本身不合法(步骤号乱序、依赖指向不存在的步骤、引用路径写错)。

    和「执行失败」是两回事:计划不合法就不该产生 run,
    所以它发生在登记阶段,而不是跑了一半才炸。
    """

    code = "invalid_plan"


class PlannerUnavailable(DomainError):
    """规划器不可用(模型服务超时 / 没配凭据)。

    刻意**不静默降级**到规则规划器:审计里写着"模型提议",实际却是规则拼出来的,
    这种记录比没有记录更糟。宁可这次请求失败,也不要一条说谎的审计。
    """

    code = "planner_unavailable"


class ProposalRejected(DomainError):
    """模型输出反复不合规。context["attempts"] 记录试了几次、每次错在哪。"""

    code = "proposal_rejected"


class PolicyDenied(DomainError):
    """策略引擎拒绝了这次操作。context 里带 rule,便于前端与复盘定位是哪条规则。"""

    code = "policy_denied"
