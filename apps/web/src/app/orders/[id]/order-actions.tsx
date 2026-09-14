"use client";

import type { OrderStatus } from "@guardrail/contracts";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ApiErrorNotice } from "@/components/api-error-notice";
import { cancelOrder, orderAction } from "@/lib/api";

const ACTIONS_BY_STATUS: Record<OrderStatus, ("pay" | "ship" | "complete")[]> = {
  CREATED: ["pay"],
  PAID: ["ship"],
  SHIPPED: ["complete"],
  COMPLETED: [],
  CANCELLED: [],
};

const LABELS = { pay: "支付", ship: "发货", complete: "完成" } as const;

const CANCELLABLE: OrderStatus[] = ["CREATED", "PAID"];

export function OrderActions({ orderId, status }: { orderId: number; status: OrderStatus }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [askReason, setAskReason] = useState(false);
  const [reason, setReason] = useState("");

  async function run(task: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await task();
      setAskReason(false);
      setReason("");
      router.refresh();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  const actions = ACTIONS_BY_STATUS[status];
  const cancellable = CANCELLABLE.includes(status);

  if (actions.length === 0 && !cancellable) {
    return (
      <section className="rounded-lg border border-line bg-surface p-4 text-sm text-subtle">
        订单已进入终态,没有可执行的状态流转。要动钱请走 Agent 操作台发起退款申请。
      </section>
    );
  }

  return (
    <section className="space-y-3 rounded-lg border border-line bg-surface p-4">
      <div className="flex flex-wrap items-center gap-2">
        {actions.map((action) => (
          <button
            key={action}
            type="button"
            disabled={busy}
            onClick={() => run(() => orderAction(orderId, action, "operator-01"))}
            className="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            {LABELS[action]}
          </button>
        ))}
        {cancellable && !askReason && (
          <button
            type="button"
            disabled={busy}
            onClick={() => setAskReason(true)}
            className="rounded-md border border-rose-900 px-3 py-1.5 text-sm text-rose-300 hover:bg-rose-950/40 disabled:opacity-50"
          >
            取消订单
          </button>
        )}
      </div>

      {askReason && (
        <div className="flex flex-wrap gap-2">
          <input
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="取消原因(会写进订单与审计)"
            className="min-w-64 flex-1 rounded-md border border-line-strong bg-canvas px-3 py-1.5 text-sm outline-none focus:border-brand"
          />
          <button
            type="button"
            disabled={busy || reason.trim().length === 0}
            onClick={() => run(() => cancelOrder(orderId, { reason }, "operator-01"))}
            className="rounded-md bg-rose-700 px-3 py-1.5 text-sm text-white hover:bg-rose-600 disabled:opacity-50"
          >
            确认取消
          </button>
          <button
            type="button"
            onClick={() => setAskReason(false)}
            className="rounded-md px-3 py-1.5 text-sm text-muted hover:text-ink"
          >
            放弃
          </button>
        </div>
      )}

      {error !== null && <ApiErrorNotice error={error} />}
    </section>
  );
}

