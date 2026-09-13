"""给 LLM 规划器的提示词。

三条原则,别的都可以改,这三条不行:

1. **模型做决策,不做检索。** 订单主键由代码解析后写死在 `<order_context>` 里,
   模型没有机会"回忆"出一个不存在的订单号。
2. **工具清单来自声明。** 用 `registry.describe()` 渲染,新增工具自动出现在提示词里,
   不会出现"代码加了工具、模型不知道"的漂移 —— 这正是"工具声明即护栏"的兑现。
3. **注入领域上下文是为了给模型拒绝的余地。** 只看到"帮我退 500 元"的模型,
   除了照单全收没有别的选择;看到"可退额度 80 元"的模型才能说出"超出额度"。

提示词只负责"让模型有机会做对"。做没做对由 `llm.py` 的校验和策略引擎判定 ——
提示词是建议,校验器才是约束。
"""

import json
from typing import Any

SYSTEM_PROMPT = """你是电商售后系统的写操作规划器。你的输出会被程序逐字段校验,
不合格的计划不会被执行,只会被退回给你重写。所以不要为了"看起来完整"而编造任何东西。

【任务】
读用户的一句话,规划出**一到多步**的执行计划,并给出每一步的工具与入参。

【输出格式】
只输出一个 JSON 对象,不要 markdown 代码块,不要多余的解释文字:
{"goal": "这次执行要达成什么,中文一句话",
 "steps": [
   {"seq": 1, "action": "工具名", "arguments": {...}, "depends_on": [],
    "rationale": "中文,一句话,给审批人看", "evidence": ["你依据的事实"], "confidence": 0.0}
 ],
 "rationale": "为什么这么编排", "confidence": 0.0}

【硬性规则】
1. action 必须是 <tools> 里出现过的名字,禁止编造工具。
2. arguments 必须满足对应工具 parameters 的 JSON Schema:
   必填字段不能少,不能出现 Schema 之外的字段,枚举值必须从 enum 里取。
3. 订单主键只能取 <order_context> 里的值,禁止自行编造、替换或推算。
4. 不要输出 risk_level / requires_approval —— 风险等级与是否需要审批由系统裁定,
   你填了也会被丢弃。
5. confidence 是你真实的把握程度:证据不足就写低一点,不要假装确定。
6. 如果 <order_context> 表明这次操作会被业务规则拒绝(状态不允许、已无可退额度、
   金额超出可退额度),仍然输出你判断最合理的计划,但必须在 rationale 里
   明确写出会被拒绝的原因,并把 confidence 调低。
7. 用户没有说明的信息不要替他补全。退款金额没说 ≠ 可以随便定一个数字,
   不传 amount_cents 就表示按可退额度全额申请。

【多步规则】
8. seq 从 1 开始,连续递增,不允许跳号或重复。
9. depends_on 只能指向**比当前步更早**的 seq;没有依赖就写 []。
10. 需要用到前面步骤的产出时,把参数写成 {"$ref": "1.字段名"}
    (例如 {"amount_cents": {"$ref": "1.available_refund_cents"}})。
    只能引用更早的步骤,只能引用标量字段 —— 不能把整个对象当成参数值。
    不要在字符串里做拼接,引用必须是一个完整的值。
11. **能用一步解决就别拆成多步。** 多步只在确实存在先后关系或数据依赖时才用:
    例如金额要先用只读工具查出来、再拿去申请退款。
"""


def extract_json(raw: str) -> Any:
    """从模型输出里取出 JSON。

    宽容地处理三种常见污染:markdown 代码块、前后废话、多输出了一段。
    这是个纯解析函数 —— 它宽容,但校验器不宽容。
    """
    text = raw.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("输出里找不到 JSON 对象")
    return json.loads(text[start : end + 1])


def render_tools(tools: list[dict[str, Any]]) -> str:
    """工具清单直接来自声明,不手写。"""
    return json.dumps(tools, ensure_ascii=False, indent=2)


def render_order_context(context: dict[str, Any]) -> str:
    return json.dumps(context, ensure_ascii=False, indent=2)


def build_user_prompt(*, intent: str, context: dict[str, Any], tools: list[dict[str, Any]]) -> str:
    return (
        f"<intent>\n{intent}\n</intent>\n\n"
        f"<order_context>\n{render_order_context(context)}\n</order_context>\n\n"
        f"<tools>\n{render_tools(tools)}\n</tools>"
    )


def build_repair_prompt(error: str) -> str:
    """把校验错误原样回喂 —— 模型要能看到"错在哪",否则重试只是重新掷骰子。"""
    return (
        f"你上一次的输出没有通过校验:\n{error}\n\n"
        "请只输出修正后的 JSON 对象(仍然包含 goal / steps),不要解释。"
    )
