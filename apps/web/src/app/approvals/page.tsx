import { ApiErrorNotice } from "@/components/api-error-notice";
import { PageHeader } from "@/components/ui/page-header";
import { listTickets } from "@/lib/api";

import { ApprovalsClient } from "./approvals-client";

const PAGE_SIZE = 20;

export default async function ApprovalsPage() {
  let tickets = null;
  let total = 0;
  let error: unknown = null;
  try {
    const page = await listTickets({ status: "PENDING", limit: PAGE_SIZE });
    tickets = page.items;
    total = page.total;
  } catch (cause) {
    error = cause;
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="审批审计中心"
        lead={
          <>
            高风险操作在这一步停下等人拍板。审批人看到的是
            <span className="text-ink">完整上下文</span>
            :打算做什么、依据什么、影响多少钱。
            <span className="text-ink">签字是记名的</span>
            —— 谁先签,责任就是谁的。
          </>
        }
      />

      {tickets ? (
        <ApprovalsClient initialTickets={tickets} initialTotal={total} />
      ) : (
        <ApiErrorNotice error={error} />
      )}
    </div>
  );
}
