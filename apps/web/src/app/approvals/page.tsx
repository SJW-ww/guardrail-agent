import type { TicketSummary } from "@guardrail/contracts";

import { ApiErrorNotice } from "@/components/api-error-notice";
import { listTickets } from "@/lib/api";

import { ApprovalsClient } from "./approvals-client";

export default async function ApprovalsPage() {
  let tickets: TicketSummary[] | null = null;
  let error: unknown = null;
  try {
    tickets = (await listTickets({ status: "PENDING", limit: 50 })).items;
  } catch (cause) {
    error = cause;
  }

  return (
    <div className="space-y-6">
      <section className="space-y-2">
        <h1 className="text-xl font-semibold">审批审计中心</h1>
        <p className="max-w-3xl text-sm leading-relaxed text-neutral-400">
          高风险操作在这一步停下等人拍板。审批人看到的是
          <span className="text-neutral-200">完整上下文</span>
          :打算做什么、依据什么、影响多少钱。审批动作本身也进审计。
        </p>
      </section>

      {tickets ? (
        <ApprovalsClient initialTickets={tickets} />
      ) : (
        <ApiErrorNotice error={error} />
      )}
    </div>
  );
}

