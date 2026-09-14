import { TRUST_LEVELS } from "@guardrail/contracts";
import Link from "next/link";

import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { PageHeader } from "@/components/ui/page-header";
import { Table, TBody, Td, Th, THead, Tr } from "@/components/ui/table";
import { PUBLIC_API_BASE_URL, getHealth } from "@/lib/api";

async function probeApi(): Promise<string> {
  try {
    const health = await getHealth();
    return health.status === "ok" ? "ok" : "unknown";
  } catch {
    return "unreachable";
  }
}

/** 治理链路的五个环节。首页先给地图,再给细节。 */
const PIPELINE = [
  { step: "提议", detail: "模型输出结构化计划,拿不到数据库执行权" },
  { step: "裁决", detail: "工具风险 × 执行体信任等级,给出理由" },
  { step: "审批", detail: "高风险停下来等人,签字记名、大额双人" },
  { step: "执行", detail: "单步单事务:业务写 + 审计 + 幂等 + 进度一次提交" },
  { step: "恢复", detail: "崩溃从断点续跑;撤不干净就一步都不撤" },
];

const ENTRY = [
  { href: "/console", label: "Agent 操作台", detail: "输入一句话,生成一条带裁决的提议" },
  { href: "/approvals", label: "审批审计中心", detail: "看谁签的字、还差几个人" },
  { href: "/governance", label: "执行与审计", detail: "看每次写操作改了什么、能不能续跑" },
];

export default async function HomePage() {
  const apiStatus = await probeApi();
  const apiOk = apiStatus === "ok";

  return (
    <div className="space-y-8">
      <PageHeader
        title="让 Agent 从「只能看」,安全地走到「能够改」"
        lead="企业不敢让 Agent 改生产数据,卡点不是模型能力,而是信任与责任没有载体。这里是 Agent 写操作的可信执行底座:模型只输出结构化提议,由策略引擎裁决,再经事务执行器落地 —— 全程幂等、可审计、可审批、可补偿。"
        right={
          <>
            <Chip tone={apiOk ? "success" : "danger"} mono title={PUBLIC_API_BASE_URL}>
              API {apiOk ? "ok" : apiStatus}
            </Chip>
            <Chip tone="brand">W1–W8 全部落地</Chip>
          </>
        }
      />

      <section className="space-y-3">
        <h2 className="text-sm font-semibold tracking-wide text-subtle uppercase">从哪开始</h2>
        <div className="grid gap-3 md:grid-cols-3">
          {ENTRY.map((item, index) => (
            <Link key={item.href} href={item.href} className="group">
              <Card className="h-full transition-colors group-hover:border-line-strong">
                <CardBody className="space-y-1.5">
                  <span className="font-mono text-xs text-brand">0{index + 1}</span>
                  <p className="text-sm font-medium text-ink">{item.label}</p>
                  <p className="text-xs leading-relaxed text-subtle">{item.detail}</p>
                </CardBody>
              </Card>
            </Link>
          ))}
        </div>
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold tracking-wide text-subtle uppercase">
          一次写操作要过几道关
        </h2>
        <Card>
          <CardBody className="grid gap-x-6 gap-y-4 md:grid-cols-5">
            {PIPELINE.map((item, index) => (
              <div key={item.step} className="space-y-1">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-xs text-brand">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                  <span className="text-sm font-medium text-ink">{item.step}</span>
                </div>
                <p className="text-xs leading-relaxed text-subtle">{item.detail}</p>
              </div>
            ))}
          </CardBody>
        </Card>
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold tracking-wide text-subtle uppercase">
          五级灰度(Trust Ladder)
        </h2>
        <p className="max-w-3xl text-xs leading-relaxed text-subtle">
          等级授予的是<span className="text-ink">执行体</span>(平台有多信任它),不是这次操作有多危险 ——
          后者由工具声明回答。模型不能自评,调用方也不能传参。
        </p>
        <Table>
          <THead>
            <Th className="w-16">级别</Th>
            <Th>形态</Th>
            <Th className="w-40">谁执行</Th>
          </THead>
          <TBody>
            {TRUST_LEVELS.map((row) => (
              <Tr key={row.level}>
                <Td mono className="text-brand">
                  {row.level}
                </Td>
                <Td className="text-ink">{row.label}</Td>
                <Td className="text-subtle">{row.executor}</Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold tracking-wide text-subtle uppercase">三条设计原则</h2>
        <div className="grid gap-3 md:grid-cols-3">
          {[
            ["模型只提议,系统做决策", "模型永远拿不到数据库执行权"],
            ["不给 SQL,只给受限领域工具", "工具声明即护栏"],
            ["每一次写操作都可审计、可回滚、可重放", "补偿与幂等都在同一个账本上"],
          ].map(([title, detail]) => (
            <Card key={title}>
              <CardHeader title={title} />
              <CardBody className="text-xs leading-relaxed text-subtle">{detail}</CardBody>
            </Card>
          ))}
        </div>
      </section>
    </div>
  );
}
