import { notFound } from "next/navigation";

import { ApiErrorNotice } from "@/components/api-error-notice";
import { StepBadge } from "@/components/status-badge";
import { ApiError, getRun, listAudit } from "@/lib/api";
import { diffSnapshots, describeValue } from "@/lib/diff";
import { formatCents, formatDateTime } from "@/lib/format";

import { RunActions } from "./run-actions";

type PolicyInfo = {
  decision: string;
  rule: string;
  reason: string;
  trust_level: string;
};

/** checkpoint.policy 是策略引擎挂起这一步时写下的裁决 —— 审批人要先看到理由。 */
function readPolicy(checkpoint: Record<string, unknown> | null | undefined): PolicyInfo | null {
  const raw = checkpoint?.policy;
  if (!raw || typeof raw !== "object") return null;
  const policy = raw as Record<string, unknown>;
  if (typeof policy.reason !== "string") return null;
  return {
    decision: String(policy.decision ?? ""),
    rule: String(policy.rule ?? ""),
    reason: policy.reason,
    trust_level: String(policy.trust_level ?? ""),
  };
}

export default async function RunDetailPage({ params }: { params: Promise<{ runUid: string }> }) {
  const { runUid } = await params;

  try {
    const [run, audit] = await Promise.all([getRun(runUid), listAudit({ run_uid: runUid })]);
    const total = run.plan.length;
    // waiting_ref 形如 step:3:create_refund —— 页面用它把「批准哪一步」显示清楚
    const waitingSeq = run.waiting_ref?.startsWith("step:")
      ? Number(run.waiting_ref.split(":")[1])
      : null;
    const policy = readPolicy(run.checkpoint);

    return (
      <div className="space-y-6">
        <section className="space-y-2">
          <p className="font-mono text-xs text-neutral-500">执行 {run.run_uid}</p>
          <h1 className="text-xl font-semibold">{run.goal}</h1>
          <p className="text-sm text-neutral-400">
            断点 {run.checkpoint_seq} / {total} 步 · actor={run.actor} · trace=
            <span className="font-mono">{run.trace_id.slice(0, 12)}</span> · 领取次数 {run.attempt}
          </p>
          {run.last_error && (
            <p className="rounded-md border border-rose-900 bg-rose-950/30 px-3 py-2 text-sm text-rose-300">
              最后一次失败:{run.last_error}
            </p>
          )}
        </section>

        {policy && (
          <section className="space-y-1 rounded-lg border border-amber-900/60 bg-amber-950/20 px-4 py-3">
            <p className="text-xs font-mono text-amber-400">
              策略裁决 {policy.decision}
              {policy.trust_level && ` · 执行体信任等级 ${policy.trust_level}`}
              {policy.rule && ` · 规则 ${policy.rule}`}
            </p>
            <p className="text-sm text-amber-200/90">{policy.reason}</p>
          </section>
        )}

        <RunActions
          runUid={run.run_uid}
          status={run.status}
          steps={run.steps}
          waitingSeq={waitingSeq}
        />

        <section className="space-y-3">
          <h2 className="text-sm font-semibold text-neutral-300">步骤</h2>
          <div className="overflow-hidden rounded-lg border border-neutral-800">
            <table className="w-full text-left text-sm">
              <thead className="bg-neutral-900/60 text-xs uppercase tracking-wide text-neutral-500">
                <tr>
                  <th className="px-4 py-2 font-medium">#</th>
                  <th className="px-4 py-2 font-medium">工具</th>
                  <th className="px-4 py-2 font-medium">状态</th>
                  <th className="px-4 py-2 font-medium">尝试</th>
                  <th className="px-4 py-2 font-medium">幂等键</th>
                  <th className="px-4 py-2 font-medium">备注</th>
                </tr>
              </thead>
              <tbody>
                {run.steps.map((step) => (
                  <tr key={step.seq} className="border-t border-neutral-800/70 align-top">
                    <td className="px-4 py-2 font-mono text-neutral-400">{step.seq}</td>
                    <td className="px-4 py-2 font-mono text-neutral-200">{step.tool_name}</td>
                    <td className="px-4 py-2">
                      <StepBadge status={step.status} />
                    </td>
                    <td className="px-4 py-2 font-mono text-xs text-neutral-400">{step.attempt}</td>
                    <td className="px-4 py-2 font-mono text-[11px] text-neutral-600">
                      {step.idempotency_key ? step.idempotency_key.slice(0, 10) : "—"}
                    </td>
                    <td className="px-4 py-2 text-xs text-neutral-500">
                      {step.error
                        ? step.error
                        : step.replayed
                          ? "幂等重放:复用历史结果,未再次写库"
                          : ""}
                    </td>
                  </tr>
                ))}
                {run.steps.length === 0 && (
                  <tr className="border-t border-neutral-800/70">
                    <td colSpan={6} className="px-4 py-6 text-center text-neutral-500">
                      还没有步骤被提交
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-neutral-500">
            计划的全部步骤:{run.plan.map((item) => `#${item.seq} ${item.tool}`).join(" · ")}
          </p>
        </section>

        <section className="space-y-3">
          <h2 className="text-sm font-semibold text-neutral-300">这次执行产生的审计</h2>
          {audit.items.map((entry) => (
            <article key={entry.id} className="space-y-2 rounded-lg border border-neutral-800 bg-neutral-900/40 p-4">
              <header className="flex flex-wrap items-center gap-3 text-xs text-neutral-500">
                <span className="font-mono text-neutral-200">{entry.tool_name}</span>
                <span>step #{entry.step_seq}</span>
                <span>{formatDateTime(entry.created_at)}</span>
              </header>
              {entry.reason && <p className="text-sm text-neutral-400">理由:{entry.reason}</p>}
              {entry.policy_reason && (
                <p className="text-xs text-neutral-500">
                  <span className="font-mono text-neutral-600">
                    策略 {entry.policy_decision}:
                  </span>{" "}
                  {entry.policy_reason}
                </p>
              )}
              <ul className="space-y-1 font-mono text-xs">
                {diffSnapshots(entry.before ?? {}, entry.after ?? {}).map((change) => (
                  <li key={change.path} className="flex flex-wrap items-baseline gap-2">
                    <span className="text-neutral-400">{change.path}</span>
                    <span className="text-rose-300/80 line-through">
                      {describeValue(change.before)}
                    </span>
                    <span className="text-neutral-600">→</span>
                    <span className="text-emerald-300">{describeValue(change.after)}</span>
                  </li>
                ))}
              </ul>
            </article>
          ))}
          {audit.items.length === 0 && (
            <p className="rounded-lg border border-neutral-800 px-4 py-6 text-center text-sm text-neutral-500">
              还没有写操作(只读步骤不写审计)
            </p>
          )}
        </section>

        <p className="text-xs text-neutral-600">
          提示:金额字段单位是「分」,展示时按 {formatCents(100)} 换算。
        </p>
      </div>
    );
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 404) notFound();
    return (
      <div className="space-y-6">
        <h1 className="text-xl font-semibold">执行详情</h1>
        <ApiErrorNotice error={cause} />
      </div>
    );
  }
}
