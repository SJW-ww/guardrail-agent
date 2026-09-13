"""执行体信任等级。

**这不是风险评级,别搞混。** 风险由工具声明(`RiskLevel`)回答"这次操作多危险";
信任等级回答的是另一个问题:"平台有多信任这个执行体"。
两者相乘才是裁决结果 —— 同一个退款工具,给淘宝客服机器人和给某银行内部助手,
该授予的信任等级不该一样。

L0 只读 → L1 建议(人点执行)→ L2 低风险可逆自动 → L3 低风险自动 + 高风险逐笔审批
→ L4 额度内受限自主,超限自动升级为人工审批。

等级由平台配置授予,不由模型自评,也不由调用方传参 —— 和给服务账号授权是一个思路。
"""

from enum import StrEnum


class TrustLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"

    @property
    def rank(self) -> int:
        return int(self.value[1:])

    def at_least(self, other: "TrustLevel") -> bool:
        return self.rank >= other.rank
