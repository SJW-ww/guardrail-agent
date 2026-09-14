import { ORDER_STATUS_LABELS, type OrderListResponse, type OrderStatus } from "@guardrail/contracts";
import Link from "next/link";

import { ApiErrorNotice } from "@/components/api-error-notice";
import { OrderBadge } from "@/components/status-badge";
import { listOrders } from "@/lib/api";
import { formatCents, formatDateTime } from "@/lib/format";

const STATUSES = Object.keys(ORDER_STATUS_LABELS) as OrderStatus[];

export default async function OrdersPage({
  searchParams,
}: {
  searchParams: Promise<{ status?: string }>;
}) {
  const { status } = await searchParams;
  const activeStatus = STATUSES.includes(status as OrderStatus)
    ? (status as OrderStatus)
    : undefined;

  let data: OrderListResponse | null = null;
  let error: unknown = null;
  try {
    data = await listOrders({ status: activeStatus, limit: 20 });
  } catch (cause) {
    error = cause;
  }

  if (!data) {
    return (
      <div className="space-y-6">
        <h1 className="text-xl font-semibold">业务后台</h1>
        <ApiErrorNotice error={error} />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h1 className="text-xl font-semibold">业务后台</h1>
        <p className="font-mono text-xs text-subtle">
          共 {data.total} 条 · 显示第 {data.offset + 1}–{data.offset + data.items.length} 条
        </p>
      </div>

      <nav className="flex flex-wrap gap-1 text-sm">
        <Link
          href="/orders"
          className={`rounded-md px-3 py-1.5 ${
            activeStatus === undefined
              ? "bg-raised text-ink"
              : "text-muted hover:bg-surface"
          }`}
        >
          全部
        </Link>
        {STATUSES.map((item) => (
          <Link
            key={item}
            href={`/orders?status=${item}`}
            className={`rounded-md px-3 py-1.5 ${
              activeStatus === item
                ? "bg-raised text-ink"
                : "text-muted hover:bg-surface"
            }`}
          >
            {ORDER_STATUS_LABELS[item]}
          </Link>
        ))}
      </nav>

      <div className="overflow-hidden rounded-lg border border-line">
        <table className="w-full text-left text-sm">
          <thead className="bg-surface text-xs uppercase tracking-wide text-subtle">
            <tr>
              <th className="px-4 py-2 font-medium">订单号</th>
              <th className="px-4 py-2 font-medium">客户</th>
              <th className="px-4 py-2 font-medium">状态</th>
              <th className="px-4 py-2 font-medium">明细</th>
              <th className="px-4 py-2 text-right font-medium">金额</th>
              <th className="px-4 py-2 font-medium">创建时间</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((item) => (
              <tr key={item.order_id} className="border-t border-line hover:bg-surface">
                <td className="px-4 py-2">
                  <Link
                    href={`/orders/${item.order_id}`}
                    className="font-mono text-emerald-400 hover:underline"
                  >
                    {item.order_no}
                  </Link>
                </td>
                <td className="px-4 py-2 text-muted">{item.customer_name}</td>
                <td className="px-4 py-2">
                  <OrderBadge status={item.status} />
                </td>
                <td className="px-4 py-2 text-subtle">{item.item_count} 项</td>
                <td className="px-4 py-2 text-right font-mono text-ink">
                  {formatCents(item.total_amount_cents)}
                </td>
                <td className="px-4 py-2 font-mono text-xs text-subtle">
                  {formatDateTime(item.created_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

