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
        <p className="max-w-3xl text-sm leading-relaxed text-neutral-400">
          当前是 <span className="font-mono text-emerald-400">L1 建议模式</span>
          :系统只生成结构化提议并由人点击执行,模型拿不到数据库执行权。
          提议由确定性规划器产出(W3 换成 LLM 时,这条链路不变)。
        </p>
      </section>

      {tools ? (
        <ConsoleClient />
      ) : (
        <ApiErrorNotice error={error} />
      )}

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">已注册的领域工具</h2>
        <div className="overflow-hidden rounded-lg border border-neutral-800">
          <table className="w-full text-left text-sm">
            <thead className="bg-neutral-900/60 text-xs uppercase tracking-wide text-neutral-500">
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
                <tr key={tool.name} className="border-t border-neutral-800/70">
                  <td className="px-4 py-2">
                    <div className="font-mono text-emerald-400">{tool.name}</div>
                    <div className="text-xs text-neutral-500">{tool.title}</div>
                  </td>
                  <td className="px-4 py-2">
                    <span
                      className={
                        tool.risk_level === "high"
                          ? "text-rose-300"
                          : tool.risk_level === "low"
                            ? "text-amber-300"
                            : "text-neutral-400"
                      }
                    >
                      {RISK_LEVEL_LABELS[tool.risk_level as "read_only" | "low" | "high"]}
                    </span>
                  </td>
                  <td className="px-4 py-2 text-neutral-400">{tool.side_effect ?? "无"}</td>
                  <td className="px-4 py-2 text-neutral-400">
                    {tool.idempotent ? tool.idempotency_key : "—"}
                  </td>
                  <td className="px-4 py-2 font-mono text-xs text-neutral-400">
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

