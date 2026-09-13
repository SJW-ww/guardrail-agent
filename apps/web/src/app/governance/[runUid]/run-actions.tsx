"use client";

import type { RunStatus, StepView } from "@guardrail/contracts";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ApiErrorNotice } from "@/components/api-error-notice";
import { approveStep, compensateRun, executeRun, retryStep } from "@/lib/api";

/**
 * 执行详情页上的审批身份。刻意用一个**不同于发起者**的人:
 * 后端会拒绝「自己批自己」,所以这个界面本身就演示了职责分离。
 *
 * 大额操作要**两个不同角色**各签一次,所以身份是可选的 ——
 * 写死一个人等于在页面上永远签不够,那这个功能就等于没有。
 */
const APPROVERS = ["supervisor-01", "supervisor-02", "finance-01"] as const;
const DEFAULT_APPROVER = APPROVERS[0];

/**
 * 推进 / 重试 / 批准。
 *
 * 「重试」是幂等账本的价值兑现点:重试一个已经成功过的步骤不会让副作用发生第二次,
 * 返回的是那一行的历史结果。页面会把「这次是真执行还是重放」明确标出来。
 */
export type ApprovalProgress = {
  required: number;
  collected: string[];
  remaining: number;
};

export function RunActions({
  runUid,
  status,
  steps,
  waitingSeq,
  approval,
}: {
  runUid: string;
  status: RunStatus;
  steps: StepView[];
  waitingSeq: number | null;
  approval: ApprovalProgress | null;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [approver, setApprover] = useState<string>(DEFAULT_APPROVER);

  const terminal = status === "SUCCEEDED" || status === "FAILED" || status === "COMPENSATED";

  async function run(key: string, task: () => Promise<unknown>) {
    setBusy(key);
    setError(null);
    setNotice(null);
    try {
      const payload = (await task()) as { replayed?: boolean };
      if (typeof payload?.replayed === "boolean") {
        setNotice(payload.replayed ? "命中幂等账本:复用历史结果,没有第二次写库" : "本次是真执行");
      }
      router.refresh();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="space-y-3 rounded-lg border border-neutral-800 bg-neutral-900/40 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={busy !== null || terminal}
          onClick={() => run("execute", () => executeRun(runUid))}
          className="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
        >
          {busy === "execute" ? "推进中…" : terminal ? "已结束" : "推进执行"}
        </button>

        {waitingSeq !== null && (
          <>
            <label htmlFor="approver" className="text-xs text-neutral-400">
              审批人身份
            </label>
            <select
              id="approver"
              value={approver}
              disabled={busy !== null}
              onChange={(event) => setApprover(event.target.value)}
              className="rounded-md border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm text-neutral-200 disabled:opacity-50"
            >
              {APPROVERS.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => run("approve", () => approveStep(runUid, waitingSeq, approver))}
              className="rounded-md bg-amber-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-500 disabled:opacity-50"
            >
              {busy === "approve"
                ? "批准中…"
                : approval && approval.required > 1
                  ? `批准第 ${waitingSeq} 步(还差 ${approval.remaining} 人,以 ${approver} 身份)`
                  : `批准第 ${waitingSeq} 步(以 ${approver} 身份)`}
            </button>
          </>
        )}

        {status === "FAILED" && (
          <button
            type="button"
            disabled={busy !== null}
            onClick={() =>
              run("compensate", async () => {
                const payload = await compensateRun(runUid, approver);
                // 契约里这两个字段带默认值,所以生成出来的类型是可选的;
                // 后端每次都显式返回,这里只是把类型边界补齐。
                const compensated = payload.compensated ?? [];
                const blockers = payload.blockers ?? [];
                const rolledBack = compensated
                  .map((seq) => `#${Math.abs(seq)}`)
                  .join("、");
                setNotice(
                  blockers.length > 0
                    ? `撤不干净,一步都没撤:${blockers.join(";")}`
                    : rolledBack
                      ? `已按声明逆序撤销:${rolledBack}`
                      : "没有需要撤销的写操作",
                );
                return payload;
              })
            }
            className="rounded-md bg-rose-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-rose-600 disabled:opacity-50"
          >
            {busy === "compensate" ? "撤销中…" : "撤销这次执行"}
          </button>
        )}

        {steps.map((step) => (
          <button
            key={step.seq}
            type="button"
            disabled={busy !== null}
            onClick={() => run(`retry-${step.seq}`, () => retryStep(runUid, step.seq))}
            className="rounded-md border border-neutral-700 px-3 py-1.5 text-sm text-neutral-300 hover:bg-neutral-800 disabled:opacity-50"
          >
            {busy === `retry-${step.seq}` ? "重试中…" : `重试 #${step.seq}`}
          </button>
        ))}
      </div>

      <p className="text-xs text-neutral-500">
        重试一个已成功的步骤只会命中幂等账本,不会产生第二次副作用 ——
        这是「可写」能上生产的前提。
      </p>
      <p className="text-xs text-neutral-500">
        「撤销这次执行」按工具声明的补偿动作逆序撤回已成功的写操作:
        撤不干净时一步都不撤,并把原因写回来(部分补偿比不补偿更难排查)。
      </p>
      {approval && approval.required > 1 && (
        <p className="text-xs text-amber-300/90">
          这一步要求 <span className="font-mono">{approval.required}</span> 人复核(大额操作),
          已签 <span className="font-mono">{approval.collected.length}</span> 人:
          <span className="font-mono">{approval.collected.join("、") || "—"}</span>
          。不同角色各签一次才算齐。
        </p>
      )}

      {notice && (
        <p className="rounded-md border border-emerald-900 bg-emerald-950/30 px-3 py-2 text-sm text-emerald-300">
          {notice}
        </p>
      )}
      {error !== null && <ApiErrorNotice error={error} />}
    </section>
  );
}
