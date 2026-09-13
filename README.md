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
| W3-W8 | 接 Agent · 策略引擎 · 审批 · 补偿 · 评测 · 开源 | ⬜ |

## 本地要求

- Python 3.12+(Docker 镜像用 3.13)
- Node 20+ / npm 10+
- Docker 24+ 与 Compose v2
