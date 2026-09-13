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
