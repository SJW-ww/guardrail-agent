import type { ToolDescription } from "@guardrail/contracts";
import { RISK_LEVEL_LABELS } from "@guardrail/contracts";

import { ApiErrorNotice } from "@/components/api-error-notice";
import { listTools } from "@/lib/api";

import { ConsoleClient } from "./console-client";

export default async function ConsolePage() {
  let tools: ToolDescription[] | null = null;
  let error: unknown = null;
  try {
    tools = await listTools();
  } catch (cause) {
    error = cause;
  }

  return (
    <div className="space-y-8">
      <section className="space-y-2">
        <h1 className="text-xl font-semibold">Agent 操作台</h1>
        <p className="max-w-3xl text-sm leading-relaxed text-muted">
          模型只产出<span className="text-ink">结构化提议</span>
          ,拿不到数据库执行权;<span className="text-ink">要不要执行、要不要人批</span>
          由策略引擎按工具风险与执行体信任等级裁决。高风险操作点下去不会立刻落库,
          而是进审批队列 —— 审批通过前业务表一个字节都不会变。
        </p>
      </section>

      {tools ? (
        <ConsoleClient />
      ) : (
        <ApiErrorNotice error={error} />
      )}

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">已注册的领域工具</h2>
        <div className="overflow-hidden rounded-lg border border-line">
          <table className="w-full text-left text-sm">
            <thead className="bg-surface text-xs uppercase tracking-wide text-subtle">
              <tr>
                <th className="px-4 py-2 font-medium">工具</th>
                <th className="px-4 py-2 font-medium">风险级</th>
                <th className="px-4 py-2 font-medium">副作用</th>
                <th className="px-4 py-2 font-medium">幂等</th>
                <th className="px-4 py-2 font-medium">补偿动作</th>
              </tr>
            </thead>
            <tbody>
              {tools?.map((tool) => (
                <tr key={tool.name} className="border-t border-line">
                  <td className="px-4 py-2">
                    <div className="font-mono text-emerald-400">{tool.name}</div>
                    <div className="text-xs text-subtle">{tool.title}</div>
                  </td>
                  <td className="px-4 py-2">
                    <span
                      className={
                        tool.risk_level === "high"
                          ? "text-rose-300"
                          : tool.risk_level === "low"
                            ? "text-amber-300"
                            : "text-muted"
                      }
                    >
                      {RISK_LEVEL_LABELS[tool.risk_level as "read_only" | "low" | "high"]}
                    </span>
                  </td>
                  <td className="px-4 py-2 text-muted">{tool.side_effect ?? "无"}</td>
                  <td className="px-4 py-2 text-muted">
                    {tool.idempotent ? tool.idempotency_key : "—"}
                  </td>
                  <td className="px-4 py-2 font-mono text-xs text-muted">
                    {tool.compensate_tool ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

