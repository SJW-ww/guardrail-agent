import { TRUST_LEVELS } from "@guardrail/contracts";
import Link from "next/link";

import { PUBLIC_API_BASE_URL, getHealth } from "@/lib/api";

async function probeApi(): Promise<string> {
  try {
    const health = await getHealth();
    return health.status === "ok" ? "ok" : "unknown";
  } catch {
    return "unreachable";
  }
}

export default async function HomePage() {
  const apiStatus = await probeApi();

  return (
    <div className="space-y-10">
      <section className="space-y-3">
        <h1 className="text-2xl font-semibold tracking-tight">
          让 Agent 从「只能看」,安全地走到「能够改」
        </h1>
        <p className="max-w-3xl text-sm leading-relaxed text-neutral-400">
          企业不敢让 Agent 改生产数据,卡点不是模型能力,而是信任与责任没有载体。本项目是 Agent
          写操作的可信执行底座:模型只输出结构化提议,由策略引擎裁决,再经事务执行器落地,全程幂等、可审计、可审批、可补偿。
        </p>
        <div className="flex flex-wrap gap-4 pt-1 font-mono text-xs">
          <span className="rounded border border-neutral-800 px-2 py-1 text-neutral-400">
            API {PUBLIC_API_BASE_URL} → <span className="text-emerald-400">{apiStatus}</span>
          </span>
          <span className="rounded border border-neutral-800 px-2 py-1 text-neutral-400">
            milestone W1 D1-D2
          </span>
        </div>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">五级灰度(Trust Ladder)</h2>
        <div className="overflow-hidden rounded-lg border border-neutral-800">
          <table className="w-full text-left text-sm">
            <thead className="bg-neutral-900/60 text-xs uppercase tracking-wide text-neutral-500">
              <tr>
                <th className="px-4 py-2 font-medium">级别</th>
                <th className="px-4 py-2 font-medium">形态</th>
                <th className="px-4 py-2 font-medium">谁执行</th>
              </tr>
            </thead>
            <tbody>
              {TRUST_LEVELS.map((row) => (
                <tr key={row.level} className="border-t border-neutral-800/70">
                  <td className="px-4 py-2 font-mono text-emerald-400">{row.level}</td>
                  <td className="px-4 py-2 text-neutral-300">{row.label}</td>
                  <td className="px-4 py-2 text-neutral-500">{row.executor}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">从哪开始</h2>
        <div className="flex flex-wrap gap-2 text-sm">
          <Link
            href="/console"
            className="rounded-md bg-emerald-600 px-3 py-1.5 font-medium text-white hover:bg-emerald-500"
          >
            Agent 操作台:生成一条退款提议
          </Link>
          <Link
            href="/approvals"
            className="rounded-md border border-neutral-800 px-3 py-1.5 text-neutral-300 hover:bg-neutral-900"
          >
            审批审计中心
          </Link>
          <Link
            href="/orders"
            className="rounded-md border border-neutral-800 px-3 py-1.5 text-neutral-300 hover:bg-neutral-900"
          >
            业务后台
          </Link>
        </div>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">三条设计原则</h2>
        <ol className="space-y-2 text-sm text-neutral-400">
          <li>
            <span className="font-mono text-emerald-400">01</span> 模型只提议,系统做决策 ——
            模型永远拿不到数据库执行权
          </li>
          <li>
            <span className="font-mono text-emerald-400">02</span> 不给 SQL,只给受限领域工具 ——
            工具声明即护栏
          </li>
          <li>
            <span className="font-mono text-emerald-400">03</span> 每一次写操作都可审计、可回滚、可重放
          </li>
        </ol>
      </section>
    </div>
  );
}
