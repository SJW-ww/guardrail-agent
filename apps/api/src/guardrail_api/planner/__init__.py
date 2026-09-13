"""规划层:把人的意图变成结构化提议。

W1-W2 用确定性 FakePlanner —— 地基没稳就接模型,出问题永远分不清是模型的锅还是工程的锅。
W3 换成 LLM 时,只需替换 `draft()` 的实现,提议的数据结构、校验与执行链路都不动。
"""

from guardrail_api.planner.fake import Proposal, draft

__all__ = ["Proposal", "draft"]
