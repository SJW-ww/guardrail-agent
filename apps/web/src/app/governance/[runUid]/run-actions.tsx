"use client";

import type { RunStatus, StepView } from "@guardrail/contracts";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ApiErrorNotice } from "@/components/api-error-notice";
import { approveStep, executeRun, retryStep } from "@/lib/api";

/**
 * 推进 / 重试 / 批准。
 *
 * 「重试」是幂等账本的价值兑现点:重试一个已经成功过的步骤不会让副作用发生第二次,
 * 返回的是那一行的历史结果。页面会把「这次是真执行还是重放」明确标出来。
 */
export function RunActions({
  runUid,
  status,
  steps,
  waitingSeq,
}: {
  runUid: string;
  status: RunStatus;
  steps: StepView[];
  waitingSeq: number | null;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);

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
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => run("approve", () => approveStep(runUid, waitingSeq))}
            className="rounded-md bg-amber-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-500 disabled:opacity-50"
          >
            {busy === "approve" ? "批准中…" : `批准第 ${waitingSeq} 步`}
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

      {notice && (
        <p className="rounded-md border border-emerald-900 bg-emerald-950/30 px-3 py-2 text-sm text-emerald-300">
          {notice}
        </p>
      )}
      {error !== null && <ApiErrorNotice error={error} />}
    </section>
  );
}
