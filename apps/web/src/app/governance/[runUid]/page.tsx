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

type ApprovalProgress = { required: number; collected: string[]; remaining: number };

type CompensationInfo = { status: string; planned: string[] };

/** checkpoint.compensation 是补偿流程留下的现场 —— 撤到哪一步、还剩几张单没撤。 */
function readCompensation(
  checkpoint: Record<string, unknown> | null | undefined,
): CompensationInfo | null {
  const raw = checkpoint?.compensation;
  if (!raw || typeof raw !== "object") return null;
  const state = raw as Record<string, unknown>;
  const status = String(state.status ?? "");
  if (!status) return null;
  const planned = Array.isArray(state.planned)
    ? state.planned.map((item) => {
        const step = item as Record<string, unknown>;
        return `${step.tool} 撤销第 ${Math.abs(Number(step.seq))} 步`;
      })
    : [];
  return { status, planned };
}

/** 人话解释四种补偿现场。撤不干净的状态必须显眼 —— 库里还有东西没撤。 */
const COMPENSATION_NOTICE: Record<string, { tone: string; text: string }> = {
  COMPENSATED: {
    tone: "border-emerald-900 bg-emerald-950/30 text-emerald-300",
    text: "这次执行的写操作已按声明逆序撤销完毕。",
  },
  NOTHING_TO_ROLLBACK: {
    tone: "border-line bg-surface text-muted",
    text: "这次执行没有产生需要撤销的写操作。",
  },
  NEEDS_APPROVAL: {
    tone: "border-amber-900 bg-amber-950/30 text-amber-300",
    text: "补偿动作在当前权限下需要人工确认:由有权限的人点「撤销这次执行」继续。",
  },
  BLOCKED: {
    tone: "border-rose-900 bg-rose-950/30 text-rose-300",
    text: "撤不干净,系统一步都没撤:有步骤没声明补偿动作或补偿被策略拒绝。写操作仍留在库里,需要人工处理。",
  },
  PARTIAL: {
    tone: "border-rose-900 bg-rose-950/30 text-rose-300",
    text: "补偿撤到一半失败了:已经撤掉的部分不会自动回滚回去,库里现在是中间状态,需要人工核对。",
  },
};

/** checkpoint.approval 是执行器挂起时写下的签名进度 —— 审批人要知道还差几个人。 */
function readApproval(
  checkpoint: Record<string, unknown> | null | undefined,
): ApprovalProgress | null {
  const raw = checkpoint?.approval;
  if (!raw || typeof raw !== "object") return null;
  const approval = raw as Record<string, unknown>;
  const required = Number(approval.required ?? 0);
  if (!Number.isFinite(required) || required < 1) return null;
  const collected = Array.isArray(approval.collected) ? (approval.collected as string[]) : [];
  return {
    required,
    collected,
    remaining: Number(approval.remaining ?? Math.max(required - collected.length, 0)),
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
    const approval = readApproval(run.checkpoint);
    const compensation = readCompensation(run.checkpoint);
    const compensationNotice = compensation ? COMPENSATION_NOTICE[compensation.status] : null;

    return (
      <div className="space-y-6">
        <section className="space-y-2">
          <p className="font-mono text-xs text-subtle">执行 {run.run_uid}</p>
          <h1 className="text-xl font-semibold">{run.goal}</h1>
          <p className="text-sm text-muted">
            断点 {run.checkpoint_seq} / {total} 步 · actor={run.actor} · trace=
            <span className="font-mono">{run.trace_id.slice(0, 12)}</span> · 领取次数 {run.attempt}
          </p>
          {run.last_error && (
            <p
              className={`rounded-md border px-3 py-2 text-sm ${
                run.status === "COMPENSATED"
                  ? "border-line bg-surface text-muted"
                  : "border-rose-900 bg-rose-950/30 text-rose-300"
              }`}
            >
              {run.status === "COMPENSATED" ? "触发补偿的原因:" : "最后一次失败:"}
              {run.last_error}
            </p>
          )}
        </section>

        {compensation && compensationNotice && (
          <section
            className={`space-y-1 rounded-lg border px-4 py-3 text-sm ${compensationNotice.tone}`}
          >
            <p className="font-mono text-xs">
              补偿 {compensation.status}
              {compensation.planned.length > 0 &&
                ` · 计划 ${compensation.planned.join(" / ")}`}
            </p>
            <p>{compensationNotice.text}</p>
          </section>
        )}

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

        {Object.entries(run.approvals ?? {}).length > 0 && (
          <section className="space-y-1 rounded-lg border border-line bg-surface px-4 py-3">
            <p className="font-mono text-xs text-subtle">审批记录(谁为哪一步签的字)</p>
            <ul className="space-y-0.5 text-sm text-muted">
              {Object.entries(run.approvals ?? {}).map(([seq, approvers]) => (
                <li key={seq}>
                  步骤 #{seq} ← <span className="font-mono">{approvers.join("、")}</span>
                  {approval?.required && approvers.length < approval.required && (
                    <span className="ml-2 text-amber-300/90">
                      还差 {approval.required - approvers.length} 人
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}

        <RunActions
          runUid={run.run_uid}
          status={run.status}
          steps={run.steps}
          waitingSeq={waitingSeq}
          approval={approval}
        />

        <section className="space-y-3">
          <h2 className="text-sm font-semibold text-muted">步骤</h2>
          <div className="overflow-hidden rounded-lg border border-line">
            <table className="w-full text-left text-sm">
              <thead className="bg-surface text-xs uppercase tracking-wide text-subtle">
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
                  <tr key={step.seq} className="border-t border-line align-top">
                    <td className="px-4 py-2 font-mono text-muted">{step.seq}</td>
                    <td className="px-4 py-2 font-mono text-ink">{step.tool_name}</td>
                    <td className="px-4 py-2">
                      <StepBadge status={step.status} />
                    </td>
                    <td className="px-4 py-2 font-mono text-xs text-muted">{step.attempt}</td>
                    <td className="px-4 py-2 font-mono text-[11px] text-subtle">
                      {step.idempotency_key ? step.idempotency_key.slice(0, 10) : "—"}
                    </td>
                    <td className="px-4 py-2 text-xs text-subtle">
                      {step.error
                        ? step.error
                        : step.replayed
                          ? "幂等重放:复用历史结果,未再次写库"
                          : ""}
                    </td>
                  </tr>
                ))}
                {run.steps.length === 0 && (
                  <tr className="border-t border-line">
                    <td colSpan={6} className="px-4 py-6 text-center text-subtle">
                      还没有步骤被提交
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-subtle">
            计划的全部步骤:{run.plan.map((item) => `#${item.seq} ${item.tool}`).join(" · ")}
          </p>
        </section>

        <section className="space-y-3">
          <h2 className="text-sm font-semibold text-muted">这次执行产生的审计</h2>
          {audit.items.map((entry) => (
            <article key={entry.id} className="space-y-2 rounded-lg border border-line bg-surface p-4">
              <header className="flex flex-wrap items-center gap-3 text-xs text-subtle">
                <span className="font-mono text-ink">{entry.tool_name}</span>
                <span>step #{entry.step_seq}</span>
                <span>{formatDateTime(entry.created_at)}</span>
              </header>
              {entry.reason && <p className="text-sm text-muted">理由:{entry.reason}</p>}
              {entry.policy_reason && (
                <p className="text-xs text-subtle">
                  <span className="font-mono text-subtle">
                    策略 {entry.policy_decision}:
                  </span>{" "}
                  {entry.policy_reason}
                </p>
              )}
              <ul className="space-y-1 font-mono text-xs">
                {diffSnapshots(entry.before ?? {}, entry.after ?? {}).map((change) => (
                  <li key={change.path} className="flex flex-wrap items-baseline gap-2">
                    <span className="text-muted">{change.path}</span>
                    <span className="text-rose-300/80 line-through">
                      {describeValue(change.before)}
                    </span>
                    <span className="text-subtle">→</span>
                    <span className="text-emerald-300">{describeValue(change.after)}</span>
                  </li>
                ))}
              </ul>
            </article>
          ))}
          {audit.items.length === 0 && (
            <p className="rounded-lg border border-line px-4 py-6 text-center text-sm text-subtle">
              还没有写操作(只读步骤不写审计)
            </p>
          )}
        </section>

        <p className="text-xs text-subtle">
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
