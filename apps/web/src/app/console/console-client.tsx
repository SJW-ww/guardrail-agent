"use client";

import {
  REASON_CODE_LABELS,
  RISK_LEVEL_LABELS,
  type ExecuteRunResponse,
  type Plan,
  type RiskLevel,
  type RunDetail,
} from "@guardrail/contracts";
import Link from "next/link";
import { useState } from "react";

import { ApiErrorNotice } from "@/components/api-error-notice";
import { createRun, draftProposal, executeRun } from "@/lib/api";
import { formatCents } from "@/lib/format";

const EXAMPLES = [
  "订单 SO2026000001 质量有问题,帮我退 80 元",
  "订单 SO2026000002 质量有问题,帮我退全款",
  "帮我看看订单 SO2026000003 的物流到哪了",
];

type Trace = { label: string; detail: string; status: "done" | "failed" };

const ARGUMENT_LABELS: Record<string, string> = {
  order_id: "订单 ID",
  order_no: "订单号",
  reason_code: "退款原因",
  amount_cents: "退款金额",
  description: "补充说明",
};

function describeArgument(key: string, value: unknown): string {
  if (key === "amount_cents" && typeof value === "number") return formatCents(value);
  if (key === "reason_code" && typeof value === "string") {
    return `${REASON_CODE_LABELS[value] ?? value}(${value})`;
  }
  if (value !== null && typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function ConsoleClient() {
  const [intent, setIntent] = useState(EXAMPLES[0]);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [run, setRun] = useState<RunDetail | null>(null);
  const [execution, setExecution] = useState<ExecuteRunResponse | null>(null);
  const [trace, setTrace] = useState<Trace[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<"draft" | "execute" | null>(null);

  function fail(label: string, cause: unknown) {
    const message = cause instanceof Error ? cause.message : "未知错误";
    setError(cause);
    setTrace((prev) => [...prev, { label, detail: message, status: "failed" }]);
  }

  async function handleDraft() {
    setBusy("draft");
    setError(null);
    setPlan(null);
    setRun(null);
    setExecution(null);
    setTrace([]);
    try {
      const drafted = await draftProposal(intent);
      setPlan(drafted);
      setTrace([
        { label: "解析意图", detail: "已从自然语言中提取操作与订单", status: "done" },
        {
          label: "生成计划",
          detail: `${drafted.steps.length} 步:${drafted.steps
            .map((step) => `${step.seq}.${step.action}`)
            .join(" → ")}`,
          status: "done",
        },
      ]);
    } catch (cause) {
      fail("解析意图", cause);
    } finally {
      setBusy(null);
    }
  }

  /**
   * 提交的是**整份计划**,不是一个工具调用。
   * 依赖与参数引用都由后端在执行时解析(前端不解释 `$ref`,否则两处解释迟早不一致)。
   */
  async function handleExecute() {
    if (!plan) return;
    setBusy("execute");
    setError(null);
    try {
      const created = await createRun({
        goal: plan.goal,
        steps: plan.steps.map((step) => ({
          seq: step.seq,
          tool: step.action,
          args: step.arguments,
          requires_approval: step.requires_approval,
          depends_on: step.depends_on,
        })),
        actor: "operator-01",
      });
      setRun(created);
      const result = await executeRun(created.run_uid);
      setExecution(result);

      const executed = result.executed ?? [];
      const replayed = result.replayed ?? [];
      const written =
        result.run.status === "WAITING_APPROVAL"
          ? "被策略引擎拦下:等人工审批,业务数据尚未改动"
          : `已执行 ${executed.length} 步${replayed.length > 0 ? `,重放 ${replayed.length} 步` : ""}`;
      setTrace((prev) => [
        ...prev,
        { label: "参数校验", detail: "JSON Schema 校验通过(引用在解析后校验)", status: "done" },
        { label: "策略裁决", detail: "逐步骤裁决:ALLOW / REQUIRE_APPROVAL", status: "done" },
        { label: "执行计划", detail: written, status: "done" },
      ]);
    } catch (cause) {
      fail("提交计划", cause);
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

      {plan && (
        <article className="space-y-3 rounded-lg border border-emerald-900/60 bg-emerald-950/10 p-4">
          <header className="flex flex-wrap items-center gap-2">
            <h2 className="text-sm font-semibold text-neutral-200">提议卡片</h2>
            <span className="rounded bg-neutral-800 px-2 py-0.5 font-mono text-xs text-emerald-300">
              {plan.steps.length} 步计划
            </span>
            {plan.steps.some((step) => step.requires_approval) && (
              <span className="rounded bg-amber-500/10 px-2 py-0.5 text-xs text-amber-300">
                需人工审批
              </span>
            )}
          </header>

          <p className="text-sm text-neutral-300">{plan.goal}</p>

          <ol className="space-y-3">
            {plan.steps.map((step) => (
              <li
                key={step.seq}
                className="space-y-2 rounded-md border border-neutral-800 bg-neutral-950/50 p-3"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-xs text-neutral-500">#{step.seq}</span>
                  <span className="rounded bg-neutral-800 px-2 py-0.5 font-mono text-xs text-emerald-300">
                    {step.action}
                  </span>
                  <span
                    className={`rounded px-2 py-0.5 text-xs ${
                      step.risk_level === "high"
                        ? "bg-rose-500/10 text-rose-300"
                        : "bg-neutral-800 text-neutral-400"
                    }`}
                  >
                    {RISK_LEVEL_LABELS[step.risk_level as RiskLevel] ?? step.risk_level}
                  </span>
                  {step.requires_approval && (
                    <span className="rounded bg-amber-500/10 px-2 py-0.5 text-xs text-amber-300">
                      需人工审批
                    </span>
                  )}
                  {(step.depends_on ?? []).length > 0 && (
                    <span className="rounded bg-neutral-800 px-2 py-0.5 text-xs text-neutral-400">
                      依赖第 {(step.depends_on ?? []).join("、")} 步
                    </span>
                  )}
                </div>

                {step.policy_reason && (
                  <p className="rounded-md border border-neutral-800 bg-neutral-950/60 px-3 py-2 text-xs text-neutral-400">
                    <span className="font-mono text-neutral-500">
                      策略裁决 {step.policy_decision}
                    </span>
                    <span className="mx-2 text-neutral-700">|</span>
                    {step.policy_reason}
                  </p>
                )}

                <dl className="grid gap-2 text-sm sm:grid-cols-2">
                  {Object.entries(step.arguments).map(([key, value]) => (
                    <div key={key} className="flex gap-2">
                      <dt className="w-24 shrink-0 text-neutral-500">
                        {ARGUMENT_LABELS[key] ?? key}
                      </dt>
                      <dd className="font-mono text-neutral-200">
                        {describeArgument(key, value)}
                        {value !== null && typeof value === "object" && (
                          <span className="ml-2 text-xs text-neutral-500">← 上游步骤的产出</span>
                        )}
                      </dd>
                    </div>
                  ))}
                </dl>

                <p className="text-sm text-neutral-300">{step.rationale}</p>
                {(step.evidence ?? []).length > 0 && (
                  <ul className="list-inside list-disc text-xs text-neutral-500">
                    {(step.evidence ?? []).map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ol>

          <button
            type="button"
            disabled={busy !== null || execution !== null}
            onClick={handleExecute}
            className="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            {busy === "execute"
              ? "提交中…"
              : plan.steps.some((step) => step.requires_approval)
                ? "提交并等待审批"
                : "执行这条提议"}
          </button>
        </article>
      )}

      {execution && run && (
        <article className="space-y-3 rounded-lg border border-neutral-800 bg-neutral-900/40 p-4">
          <header className="flex items-center gap-2">
            <h2 className="text-sm font-semibold text-neutral-200">执行结果</h2>
            <span className="font-mono text-xs text-neutral-500">
              run={run.run_uid.slice(0, 8)} · actor={run.actor} · {execution.run.status}
            </span>
          </header>
          {execution.run.status === "WAITING_APPROVAL" ? (
            <>
              <p className="rounded-md border border-amber-900 bg-amber-950/30 px-3 py-2 text-sm text-amber-300">
                策略引擎把这次写操作挂起了:需人工审批,业务数据尚未改动。
                已执行 {(execution.executed ?? []).length} 步,剩下的一步在等签字。
              </p>
              <Link
                href={`/governance/${run.run_uid}`}
                className="inline-block text-sm text-emerald-400 hover:underline"
              >
                去执行详情审批这一步 →
              </Link>
            </>
          ) : (
            <>
              <p className="text-sm text-emerald-300">
                计划已跑完:{(execution.executed ?? []).length} 步执行
                {(execution.replayed ?? []).length > 0 &&
                  `,${(execution.replayed ?? []).length} 步命中幂等账本`}。
              </p>
              <Link
                href={`/governance/${run.run_uid}`}
                className="inline-block text-sm text-emerald-400 hover:underline"
              >
                看这次执行的步骤与审计 →
              </Link>
            </>
          )}
        </article>
      )}

      {trace.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold text-neutral-300">
            执行轨迹
            <span className="ml-2 font-mono text-xs font-normal text-neutral-500">
              每一步都落在 run / step 表里,可查可续跑
            </span>
          </h2>
          <ol className="space-y-1 font-mono text-xs">
            {trace.map((item, index) => (
              <li key={`${item.label}-${index}`} className="flex gap-3">
                <span className={item.status === "done" ? "text-emerald-400" : "text-rose-400"}>
                  {item.status === "done" ? "✓" : "✗"}
                </span>
                <span className="w-32 shrink-0 text-neutral-400">{item.label}</span>
                <span className="text-neutral-500">{item.detail}</span>
              </li>
            ))}
          </ol>
        </section>
      )}
    </section>
  );
}
