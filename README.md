# GuardRail · Agent 写操作安全执行层

> 让 Agent 从"只能看",安全地走到"能够改"。

针对 **"Agent 能读不能写、企业不敢授权它修改生产数据"** 的落地瓶颈,构建一层写操作的可信执行底座:
模型只输出结构化提议,由策略引擎按 权限 / 额度 / 频次 / 状态机合法性 裁决,再经事务执行器落地;
全程幂等、可审计、可审批、可补偿、可灰度放开。

这不是一个业务系统,是一层**基础设施**。业务场(电商订单售后)只是试验场,做薄。

## 三条设计原则

1. **模型只提议,系统做决策。** 模型永远拿不到数据库执行权。
2. **不给 SQL,只给受限领域工具。** 工具声明即护栏:参数 Schema + 取值域 + 前置条件 + 副作用声明。
3. **每一次写操作都可审计、可回滚、可重放。**

## 五级灰度(Trust Ladder)

| 级别 | 形态 | 谁执行 |
| --- | --- | --- |
| L0 | 只读查询 / 答疑 | — |
| L1 | Agent 出建议卡片,人点执行 | 人 |
| L2 | 低风险可逆操作自动执行(建工单 / 改地址) | Agent + 审计 |
| L3 | 高风险操作 Agent 备好 → 人审批 → 系统执行(退款 / 改价) | Agent 提议,人批,系统落地 |
| L4 | 额度 / 频次 / 白名单内受限自主,超限自动升级到 L3 | Agent |

## 领域工具(模型唯一被允许触碰业务的方式)

| 工具 | 风险级 | 副作用 | 幂等 | 补偿动作 |
| --- | --- | --- | --- | --- |
| `query_order` | read_only | 无 | — | — |
| `query_logistics` | read_only | 无 | — | — |
| `create_refund` | **high** | 创建待审批退款工单(不动资金) | ✅ | `close_ticket` |
| `close_ticket` | low | 关闭售后工单 | ✅ | — |

每个工具用一份声明描述自己的参数 Schema、风险级、前置条件、副作用、幂等策略与补偿动作。
注册期强制校验:**写工具不声明副作用、高风险工具不声明幂等与补偿,根本无法注册**。
新增工具 = 新增一个模块,编排层与治理层不改代码。

> `create_refund` 只创建 PENDING 工单,**不产生资金变动** ——
> 这是"模型只提议,系统做决策"在工具契约上的落点。

## 架构

```
┌────── Next.js 四台(业务后台 · Agent 操作台 · 审批审计中心 · 执行与审计) ──────┐
│                              SSE / WebSocket                                │
├─────────────────────────────── 平台 API ────────────────────────────────────┤
│ ① 多 Agent      受理 → 风控 → 执行 → 审核                                    │
│ ② Harness 内核  状态机 · Checkpoint 续跑 · 幂等 · 预算 · HITL 挂起恢复        │
│ ③ 策略引擎      权限/额度/频次/状态机/时段/白名单 → ALLOW/APPROVAL/DENY+理由  │
│ ④ 工具网关(MCP) 领域工具契约 · Schema 校验 · 幂等键 · 审计 · 补偿声明         │
│ ⑤ 执行器        事务 · 乐观锁 · Saga 补偿 · 对账                             │
│ ⑥ 可观测与评测  OTel → Langfuse · 四级 trace · 指标看板 · 评测门禁           │
└─────────────────────────────────────────────────────────────────────────────┘
   业务系统(薄):商品 · 库存 · 订单 · 售后工单 · 客户 · 权限
   存储:PostgreSQL(业务 + 审计 + 幂等) · Redis(状态/队列/锁)
```

## 治理内核(W2)

"能让 Agent 改数据"和"敢让 Agent 改数据"之间的距离,全在这四张表和三条不变量上。

### 四张表

| 表 | 作用 |
| --- | --- |
| `agent_run` | 一次执行:plan / **checkpoint** / 租约 / 心跳 / 预算 |
| `agent_step` | 单步状态。**步骤粒度就是事务粒度** |
| `idempotency_key` | 幂等账本,主键 = `hash(run_uid, tool, args)` |
| `audit_log` | 追加写审计,数据库触发器禁止 `UPDATE` / `DELETE` / `TRUNCATE` |

### 三条不变量

1. **不重复** —— 写步骤先抢幂等账本的主键(`INSERT ... ON CONFLICT DO NOTHING`)。
   抢不到就复用历史结果,不再碰业务表。并发提交同一意图时由唯一索引收敛,不靠应用层的"先查再写"。
2. **不半途** —— 业务写 + 审计写 + 幂等终态 + 步骤状态**同一次提交**。
   在提交前被 `kill -9`,四者一起回滚,不会留下"步骤成功但钱没动"或"钱动了但没记录"。
3. **可续跑** —— 每一步提交后落 checkpoint。重启的 worker 从"最大的已成功 seq"之后继续,
   已完成步骤的 `attempt` 不会因为重启而增加。

### 租约

`FOR UPDATE SKIP LOCKED` 原子领取 + 30s 租约 + 10s 心跳。持租约才能推进;
心跳续不上就立刻停手(否则会在已经不属于自己的数据上继续写)。执行者死掉后,
租约到期自动被下一个副本接管 —— 这就是崩溃恢复的入口。

### 验证它,而不是相信它

```bash
make demo-governance
```

脚本会**真的**在两步之间 `SIGKILL` 掉 worker,然后重启、续跑、重放 5 次并打印审计前后值。
它跑的是和生产同一套代码,只多了一个混沌钩子。

## 策略引擎(W3)

**模型只提议,系统做决策。** 裁决发生在服务端,输入只有两样:工具声明的风险等级、
平台授予执行体的信任等级 —— 两者相乘,得出 `ALLOW` / `REQUIRE_APPROVAL` / `DENY`
外加一句给人看的理由。

|  | 只读工具 | 低风险写 | 高风险写 |
| --- | --- | --- | --- |
| L0 只读 | ALLOW | DENY | DENY |
| L1 建议 | ALLOW | REQUIRE_APPROVAL | REQUIRE_APPROVAL |
| L2 低风险可逆自动 | ALLOW | ALLOW(需声明补偿) | REQUIRE_APPROVAL |
| L3 低风险自动 + 高风险逐笔审批 | ALLOW | ALLOW | REQUIRE_APPROVAL |
| L4 额度内受限自主 | ALLOW | ALLOW | ALLOW(幂等 + 可补偿 + 额度内),超限自动升级 |

三个不妥协的地方:

1. **L 等级是"平台有多信任这个执行体",不是"这次操作有多危险"。**
   后者由工具声明回答,前者由平台配置授予 —— 模型不能自评,调用方也不能传参。
2. **高风险默认一刀切。** 只有 L4 在「幂等 + 声明了补偿动作 + 未超单笔额度」三者同时成立时
   才自动执行,少一条就升级为人工审批。放行凭据是平台配置的额度,不是模型给自己开的绿灯。
3. **裁决每次都留痕。** `audit_log.policy_decision` / `policy_reason` 记下"当时凭什么允许它写";
   等审批的 run 会**交还租约** —— 人思考的时候不该占着执行权。

### 审批:批了不算完,还要答得出「谁批的」

审批不是「点一下放行」,它是一次**有人签字**的事件。所以三件事都做对:

- **谁能批**:发起这次执行的执行体不能批自己(职责分离);
  机器执行体(`agent:` 前缀)默认不能替人签字 —— 审批的意义就在于有人负责。
  真要自动化审批,得显式打开 `POLICY_ALLOW_AGENT_APPROVAL` 并自担风险。
- **批了哪一步**:必须在这次执行的计划里,否则直接拒绝。
- **是谁批的**:`agent_run.approvals = {"1": "human:supervisor-01"}`,
  且**重复批准不覆盖** —— 谁先签字,就是谁的责任。执行时的审计行会写上
  「已获 human:supervisor-01 批准后执行」,所以「谁批的」这一份记录
  落在不可篡改的 `audit_log` 里,而不是只在可改的 run 行上。

审批通过只是**放行**,不是执行:状态回到 `PENDING`,等 worker 或调用方推进。

### 规划器:LLM 做决策,不做检索

`PLANNER_BACKEND=llm` 时接 OpenAI 兼容接口(`deterministic` 为默认,离线可跑,CI 不依赖外部服务)。

- **订单主键不由模型生成。** 先用正则把订单捞出来、从库里取回真实订单,再连同
  "当前状态 / 已退金额 / 可退额度"注入提示词 —— 让模型有**拒绝的余地**,
  而不是只能瞎算一个数字然后被系统打回。
- **工具清单来自声明。** `registry.describe()` 直接渲染进提示词,新增工具自动出现。
- **模型填的 `risk_level` / `requires_approval` 一律丢弃。** 让模型给自己的操作定风险等级,
  等于让它自己发通行证。
- 输出不合规就把**校验错误原样回喂**重写(默认 2 次)。连续不合格则整条提议被拒,而不是降级执行。

模型不可用时**不会**偷偷退回规则规划器:审计里写着"模型提议"、实际却是规则拼出来的,
这种记录比没有记录更糟。

## 快速开始

```bash
make init        # 生成 .env
make up          # 起 postgres + redis + api + web
make ps          # 看状态
make migrate     # 建表
make seed        # 灌演示数据(100 客户 / 200 商品 / 500 订单 / 200 工单)
curl localhost:8000/health
open http://localhost:3000
```

本地不用 Docker 时:

```bash
cd apps/api && uv sync && uv run uvicorn guardrail_api.main:app --reload   # 后端
npm install && npm run dev                                                # 前端
```

## 测试

```bash
make api-test            # 快速套件:不需要数据库(状态机、配置、路由契约)
make test-integration    # 集成测试:订单全链路 · 库存行锁 · 幂等 · 续跑 · 租约 · 审计
make e2e                 # 端到端:提议 → 执行 → 审批 → 退款 + 治理看板(需整个栈在跑)
make demo-governance     # 演示:kill -9 续跑 · 幂等重放 · 审计前后值
make worker              # 常驻执行 worker(可起多个副本验证租约互斥)
make check               # CI 等价:后端 lint + 测试,前端 typecheck + lint
```

集成测试需要一个独立的 `guardrail_test` 库,夹具会自动创建,**不会碰主库**。

治理层的失败注入是**真跑进程**的:测试用 `asyncio.create_subprocess_exec` 拉起 worker,
再用 `--crash-before-step` / `--crash-during-step` 让它被 `SIGKILL`,
然后断言"业务写 / 审计写 / 幂等写一起回滚"以及"重启后不重放已完成的步骤"。

前端类型不是手写的,而是从 FastAPI 的 OpenAPI 生成:

```bash
npm run contracts:export   # 导出 openapi.json 并重新生成 packages/contracts
```

## 目录结构

```
GuardRail/
├── apps/
│   ├── api/                    # FastAPI + async SQLAlchemy
│   │   ├── alembic/            # 迁移
│   │   ├── src/guardrail_api/  # 应用代码
│   │   └── tests/              # pytest
│   └── web/                    # Next.js 15 + React 19 + Tailwind
├── packages/
│   └── contracts/              # 前后端共享契约(W1 D6-D7 改为从 OpenAPI 生成)
├── deploy/                     # Dockerfile
├── docs/                       # 设计文档
└── .github/workflows/ci.yml
```

## 开发进度

| 周 | 内容 | 状态 |
| --- | --- | --- |
| W1 D1-D2 | 仓库骨架 + 运行时 + CI | ✅ |
| W1 D3-D4 | 业务系统薄模型(6 张表 + seed) | ✅ |
| W1 D5 | 领域工具层(4 个工具 + 声明式元数据) | ✅ |
| W1 D6-D7 | 前端三台 + REST 路由 + OpenAPI 契约 + 端到端 | ✅ |
| W2 | 治理内核:Checkpoint 续跑 · 幂等 · 租约 · 审计 | ✅ |
| W3 | LLM 规划器 + 策略引擎 + 审计裁决留痕 | ✅ |
| W4-W8 | 多步编排 · 补偿 Saga · 可观测与评测 · 开源 | ⬜ |

## 本地要求

- Python 3.12+(Docker 镜像用 3.13)
- Node 20+ / npm 10+
- Docker 24+ 与 Compose v2
