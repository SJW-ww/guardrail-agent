"""多步计划:步骤之间的**顺序依赖**与**参数引用**。

一个 run 的计划是一串步骤,步骤之间可以有两种关系:

- **先后**:`depends_on=[1]` —— 第 2 步依赖第 1 步**成功**;
- **取值**:`{"$ref": "1.items[0].order_item_id"}` —— 这个参数取第 1 步产出里的字段。

刻意把两者分开:顺序是**编排**语义,取值是**数据流**语义。
引用当然也要求上游已经成功,但「我要用你的产出」和「我要等你先做完」
不是同一件事 —— 大多时候只需要其中一个。合成一个概念会让计划既难读也难校验。

三条硬规则,全部在**登记执行之前**校验。计划不合法就不该产生 run:

1. `seq` 唯一且严格递增。执行顺序就是 seq 顺序,不做拓扑排序 ——
   这条规则让「无环」成为构造性事实:依赖只能指向更早的步骤,环写不出来。
2. 依赖与引用只能指向**存在且更早**的 seq。写错就在登记时报错,
   而不是跑到第二步才发现第一步根本没做完。
3. 引用只能取**标量**(字符串/数字/布尔)。把上游的 dict 整个塞进下游参数,
   等于绕过工具的参数校验 —— 那正是本项目最不想开的口子。

第 3 条有个副作用值得说明:引用在登记时**无法完整校验**(上游还没跑)。
所以含引用的步骤不做参数 Schema 预校验,留到解析之后由工具自己校验 ——
校验一次都不能少,只是晚一步做。
"""

import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from guardrail_api.domain.errors import PlanError
from guardrail_api.tools.registry import ToolRegistry

#: 参数里表示「取上游步骤产出」的键。单独一个键、单独一个字符串值,
#: 不做字符串插值:让「引用」和「普通文本」在语法上无法混淆。
REF_KEY = "$ref"

#: `1.ticket_id` / `2.items[0].order_item_id` —— 只允许点号与下标,不允许表达式。
REF_PATTERN = re.compile(r"^(\d+)\.([A-Za-z_][A-Za-z0-9_.\[\]]*)$")

#: 路径由「字段名」和「下标」两种 token 组成,中间只允许点号 —— 不支持表达式。
_PATH_TOKEN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


def is_ref(value: Any) -> bool:
    return isinstance(value, Mapping) and list(value.keys()) == [REF_KEY]


#: 计划有两种形态:执行器里的 `PlannedStep`(`tool` / `args`)和规划器输出的
#: `PlanStep`(`action` / `arguments`)。校验规则是同一套,所以这里统一取一次 ——
#: 把两张皮分开会让"规划器校验通过、执行器炸掉"成为可能。
def step_seq(step: Any) -> int:
    return int(step.seq)


def step_tool(step: Any) -> str:
    return str(getattr(step, "tool", None) or step.action)


def step_args(step: Any) -> Mapping[str, Any]:
    return getattr(step, "args", None) or getattr(step, "arguments", None) or {}


def step_deps(step: Any) -> tuple[int, ...]:
    return tuple(int(item) for item in (getattr(step, "depends_on", None) or ()))


def iter_refs(value: Any) -> Iterator[str]:
    """递归找出参数里所有引用。嵌套 dict / list 也算 —— 参数不是只有一层。"""
    if is_ref(value):
        ref = value[REF_KEY]
        if not isinstance(ref, str):
            raise PlanError(f"引用必须是字符串,收到 {ref!r}", ref=ref)
        yield ref
        return
    if isinstance(value, Mapping):
        for item in value.values():
            yield from iter_refs(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_refs(item)


def parse_ref(ref: str) -> tuple[int, list[str | int]]:
    """把 `1.items[0].order_item_id` 拆成 (1, ["items", 0, "order_item_id"])。"""
    match = REF_PATTERN.match(ref)
    if match is None:
        raise PlanError(
            f"引用写法不合法:{ref!r},应形如 `1.ticket_id` 或 `1.items[0].id`",
            ref=ref,
        )

    path: list[str | int] = []
    cursor = 0
    rest = match.group(2)
    for token in _PATH_TOKEN.finditer(rest):
        if rest[cursor : token.start()] not in ("", "."):
            raise PlanError(f"引用写法不合法:{ref!r}(路径只能由字段名、点号与下标组成)", ref=ref)
        path.append(token.group(1) if token.group(1) is not None else int(token.group(2)))
        cursor = token.end()
    if cursor != len(rest) or not path:
        raise PlanError(f"引用写法不合法:{ref!r}", ref=ref)
    return int(match.group(1)), path


def lookup(payload: Any, path: Iterable[str | int]) -> Any:
    """按路径取值。取不到就明确报错 —— 不能把上游的哪一层写错当成"参数为空"。"""
    current = payload
    walked: list[str] = []
    for key in path:
        walked.append(str(key))
        if isinstance(key, int):
            if not isinstance(current, list) or key >= len(current):
                detail = (
                    f"列表长度 {len(current)}"
                    if isinstance(current, list)
                    else type(current).__name__
                )
                raise PlanError(
                    f"引用取值失败:{'.'.join(walked)} 不存在(上游产出里这一层是 {detail})",
                    path=".".join(walked),
                )
            current = current[key]
            continue
        if not isinstance(current, Mapping) or key not in current:
            raise PlanError(
                f"引用取值失败:{'.'.join(walked)} 不存在",
                path=".".join(walked),
                available=sorted(current) if isinstance(current, Mapping) else None,
            )
        current = current[key]
    return current


def resolve(value: Any, results: Mapping[int, Mapping[str, Any]]) -> Any:
    """把参数里的引用全部替换成上游步骤的产出。

    `results` 只包含**已成功**步骤的结果 —— 引用一个没成功(或不存在)的步骤,
    是编排错误,不是"参数为空",所以这里报错而不是给个 None。
    """
    if is_ref(value):
        seq, path = parse_ref(str(value[REF_KEY]))
        if seq not in results:
            raise PlanError(
                f"引用第 {seq} 步的产出,但这一步还没有成功", seq=seq, ref=value[REF_KEY]
            )
        resolved = lookup(results[seq], path)
        if isinstance(resolved, (dict, list)):
            raise PlanError(
                f"引用 {value[REF_KEY]} 取到的是 {type(resolved).__name__},"
                "只允许引用标量值 —— 把整个结构塞进参数会绕过工具的参数校验",
                ref=value[REF_KEY],
            )
        return resolved
    if isinstance(value, Mapping):
        return {key: resolve(item, results) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, results) for item in value]
    return value


def validate(steps: list[Any], *, registry: ToolRegistry | None = None) -> None:
    """登记前校验。接受执行器的 `PlannedStep`,也接受规划器的 `PlanStep`。"""
    if not steps:
        raise PlanError("计划不能为空")

    seqs = [step_seq(step) for step in steps]
    if len(set(seqs)) != len(seqs):
        raise PlanError(f"计划里的 seq 必须唯一:{seqs}", seqs=seqs)
    if seqs != sorted(seqs):
        raise PlanError(f"计划必须按 seq 递增排列:{seqs}", seqs=seqs)

    known = set(seqs)
    for step in steps:
        seq, tool, deps, args = step_seq(step), step_tool(step), step_deps(step), step_args(step)
        if registry is not None and tool not in registry:
            raise PlanError(
                f"第 {seq} 步调用了未注册的工具 {tool}",
                seq=seq,
                tool=tool,
                available=registry.names(),
            )
        for dep in deps:
            if dep not in known:
                raise PlanError(f"第 {seq} 步依赖不存在的第 {dep} 步", seq=seq, dep=dep)
            if dep >= seq:
                raise PlanError(
                    f"第 {seq} 步依赖第 {dep} 步:依赖只能指向更早的步骤"
                    "(顺序执行 + 只允许回头依赖,是无环的构造性保证)",
                    seq=seq,
                    dep=dep,
                )
        for ref in iter_refs(args):
            target, _ = parse_ref(ref)
            if target not in known:
                raise PlanError(f"第 {seq} 步引用了不存在的第 {target} 步", seq=seq, ref=ref)
            if target >= seq:
                raise PlanError(
                    f"第 {seq} 步引用第 {target} 步的产出:引用只能指向更早的步骤",
                    seq=seq,
                    ref=ref,
                )


def unresolved_seqs(steps: list[Any]) -> set[int]:
    """含参数引用的步骤序号。这些步骤的参数校验要等解析之后再做。"""
    return {step_seq(step) for step in steps if next(iter_refs(step_args(step)), None) is not None}
