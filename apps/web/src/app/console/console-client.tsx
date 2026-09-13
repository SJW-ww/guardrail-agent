"use client";

import {
  REASON_CODE_LABELS,
  RISK_LEVEL_LABELS,
  type Proposal,
  type RiskLevel,
  type ToolInvocationResponse,
} from "@guardrail/contracts";
import Link from "next/link";
import { useState } from "react";

import { ApiErrorNotice } from "@/components/api-error-notice";
import { draftProposal, invokeTool } from "@/lib/api";
import { formatCents } from "@/lib/format";

const EXAMPLES = [
  "订单 SO2026000001 质量有问题,帮我退 80 元",
  "帮我看看订单 SO2026000002 的物流到哪了",
  "查一下订单 SO2026000003",
];

type Step = { label: string; detail: string; status: "done" | "failed" };

const ARGUMENT_LABELS: Record<string, string> = {
  order_id: "订单 ID",
  reason_code: "退款原因",
  amount_cents: "退款金额",
  description: "补充说明",
};

function describeArgument(key: string, value: unknown): string {
  if (key === "amount_cents" && typeof value === "number") return formatCents(value);
  if (key === "reason_code" && typeof value === "string") {
    return `${REASON_CODE_LABELS[value] ?? value}(${value})`;
  }
  return String(value);
}

export function ConsoleClient() {
  const [intent, setIntent] = useState(EXAMPLES[0]);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [invocation, setInvocation] = useState<ToolInvocationResponse | null>(null);
  const [steps, setSteps] = useState<Step[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<"draft" | "execute" | null>(null);

  function fail(label: string, cause: unknown) {
    const message = cause instanceof Error ? cause.message : "未知错误";
    setError(cause);
    setSteps((prev) => [...prev, { label, detail: message, status: "failed" }]);
  }

  async function handleDraft() {
    setBusy("draft");
    setError(null);
    setProposal(null);
    setInvocation(null);
    setSteps([]);
    try {
      const drafted = await draftProposal(intent);
      setProposal(drafted);
      setSteps([
        { label: "解析意图", detail: "已从自然语言中提取操作与订单", status: "done" },
        {
          label: "生成结构化提议",
          detail: `action=${drafted.action},risk=${drafted.risk_level}`,
          status: "done",
        },
      ]);
    } catch (cause) {
      fail("解析意图", cause);
    } finally {
      setBusy(null);
    }
  }

  async function handleExecute() {
    if (!proposal) return;
    setBusy("execute");
    setError(null);
    try {
      const result = await invokeTool(proposal.action, proposal.arguments, "operator-01");
      setInvocation(result);
      const written = result.read_only
        ? "只读查询,未产生副作用"
        : `已执行:${result.side_effect ?? "写操作"}`;
      setSteps((prev) => [
        ...prev,
        { label: "参数校验", detail: "JSON Schema 校验通过", status: "done" },
        { label: "执行工具", detail: written, status: "done" },
      ]);
    } catch (cause) {
      fail("执行工具", cause);
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="space-y-4">
      <div className="space-y-2 rounded-lg border border-neutral-800 bg-neutral-900/40 p-4">
        <label htmlFor="intent" className="text-sm font-medium text-neutral-300">
          你想做什么
        </label>
        <textarea
          id="intent"
          value={intent}
          rows={2}
          onChange={(event) => setIntent(event.target.value)}
          className="w-full resize-none rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm outline-none focus:border-emerald-600"
        />
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={busy !== null || intent.trim().length < 2}
            onClick={handleDraft}
            className="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            {busy === "draft" ? "生成中…" : "生成提议"}
          </button>
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              onClick={() => setIntent(example)}
              className="rounded border border-neutral-800 px-2 py-1 text-xs text-neutral-500 hover:text-neutral-300"
            >
              {example.slice(0, 16)}…
            </button>
          ))}
        </div>
      </div>

      {error !== null && <ApiErrorNotice error={error} />}

      {proposal && (
        <article className="space-y-3 rounded-lg border border-emerald-900/60 bg-emerald-950/10 p-4">
          <header className="flex flex-wrap items-center gap-2">
            <h2 className="text-sm font-semibold text-neutral-200">提议卡片</h2>
            <span className="rounded bg-neutral-800 px-2 py-0.5 font-mono text-xs text-emerald-300">
              {proposal.action}
            </span>
            <span
              className={`rounded px-2 py-0.5 text-xs ${
                proposal.risk_level === "high"
                  ? "bg-rose-500/10 text-rose-300"
                  : "bg-neutral-800 text-neutral-400"
              }`}
            >
              {RISK_LEVEL_LABELS[proposal.risk_level as RiskLevel]}
            </span>
            {proposal.requires_approval && (
              <span className="rounded bg-amber-500/10 px-2 py-0.5 text-xs text-amber-300">
                需人工审批
              </span>
            )}
          </header>

          <dl className="grid gap-2 text-sm sm:grid-cols-2">
            {Object.entries(proposal.arguments).map(([key, value]) => (
              <div key={key} className="flex gap-2">
                <dt className="w-24 shrink-0 text-neutral-500">
                  {ARGUMENT_LABELS[key] ?? key}
                </dt>
                <dd className="font-mono text-neutral-200">{describeArgument(key, value)}</dd>
              </div>
            ))}
          </dl>

          <p className="text-sm text-neutral-300">{proposal.rationale}</p>
          {proposal.evidence.length > 0 && (
            <ul className="list-inside list-disc text-xs text-neutral-500">
              {proposal.evidence.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          )}

          <button
            type="button"
            disabled={busy !== null || invocation !== null}
            onClick={handleExecute}
            className="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            {busy === "execute" ? "执行中…" : "执行这条提议"}
          </button>
        </article>
      )}

      {invocation && (
        <article className="space-y-3 rounded-lg border border-neutral-800 bg-neutral-900/40 p-4">
          <header className="flex items-center gap-2">
            <h2 className="text-sm font-semibold text-neutral-200">执行结果</h2>
            <span className="font-mono text-xs text-neutral-500">actor={invocation.actor}</span>
          </header>
          {typeof invocation.result.note === "string" && (
            <p className="text-sm text-emerald-300">{invocation.result.note}</p>
          )}
          <pre className="max-h-72 overflow-auto rounded-md bg-neutral-950 p-3 font-mono text-xs text-neutral-400">
            {JSON.stringify(invocation.result, null, 2)}
          </pre>
          {!invocation.read_only && (
            <Link href="/approvals" className="inline-block text-sm text-emerald-400 hover:underline">
              去审批中心处理这张工单 →
            </Link>
          )}
        </article>
      )}

      {steps.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold text-neutral-300">
            执行轨迹
            <span className="ml-2 font-mono text-xs font-normal text-neutral-500">
              W2 接入 run/step 持久化后替换
            </span>
          </h2>
          <ol className="space-y-1 font-mono text-xs">
            {steps.map((step, index) => (
              <li key={`${step.label}-${index}`} className="flex gap-3">
                <span className={step.status === "done" ? "text-emerald-400" : "text-rose-400"}>
                  {step.status === "done" ? "✓" : "✗"}
                </span>
                <span className="w-32 shrink-0 text-neutral-400">{step.label}</span>
                <span className="text-neutral-500">{step.detail}</span>
              </li>
            ))}
          </ol>
        </section>
      )}
    </section>
  );
}

