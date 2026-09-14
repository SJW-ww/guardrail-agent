import type { OrderView } from "@guardrail/contracts";
import Link from "next/link";

import { ApiErrorNotice } from "@/components/api-error-notice";
import { OrderBadge } from "@/components/status-badge";
import { getOrder } from "@/lib/api";
import { formatCents, formatDateTime } from "@/lib/format";

import { OrderActions } from "./order-actions";

export default async function OrderDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;

  let order: OrderView | null = null;
  let error: unknown = null;
  try {
    order = await getOrder(Number(id));
  } catch (cause) {
    error = cause;
  }

  if (!order) {
    return (
      <div className="space-y-6">
        <Link href="/orders" className="text-sm text-muted hover:text-ink">
          ← 返回订单列表
        </Link>
        <ApiErrorNotice error={error} />
      </div>
    );
  }

  const timeline: { label: string; at: string | null | undefined }[] = [
    { label: "创建", at: order.created_at },
    { label: "支付", at: order.paid_at },
    { label: "发货", at: order.shipped_at },
    { label: "完成", at: order.completed_at },
    { label: "取消", at: order.cancelled_at },
  ];

  return (
    <div className="space-y-6">
      <Link href="/orders" className="text-sm text-muted hover:text-ink">
        ← 返回订单列表
      </Link>

      <div className="flex flex-wrap items-center gap-3">
        <h1 className="font-mono text-xl font-semibold text-emerald-400">{order.order_no}</h1>
        <OrderBadge status={order.status} />
        <span className="font-mono text-sm text-muted">
          {formatCents(order.total_amount_cents)}
        </span>
      </div>

      <OrderActions orderId={order.order_id} status={order.status} />

      <div className="grid gap-6 lg:grid-cols-2">
        <section className="space-y-3 rounded-lg border border-line p-4">
          <h2 className="text-sm font-semibold text-muted">客户与收货</h2>
          <dl className="space-y-1 text-sm">
            <div className="flex gap-2">
              <dt className="w-20 text-subtle">客户</dt>
              <dd>
                {order.customer.name}
                <span className="ml-2 rounded bg-raised px-1.5 py-0.5 text-xs text-muted">
                  {order.customer.tier}
                </span>
              </dd>
            </div>
            <div className="flex gap-2">
              <dt className="w-20 text-subtle">收件人</dt>
              <dd>
                {order.receiver.name} · {order.receiver.phone}
              </dd>
            </div>
            <div className="flex gap-2">
              <dt className="w-20 text-subtle">地址</dt>
              <dd className="text-muted">{order.receiver.address}</dd>
            </div>
          </dl>
        </section>

        <section className="space-y-3 rounded-lg border border-line p-4">
          <h2 className="text-sm font-semibold text-muted">状态时间线</h2>
          <ol className="space-y-1 text-sm">
            {timeline.map((step) => (
              <li key={step.label} className="flex gap-3">
                <span className="w-10 text-subtle">{step.label}</span>
                <span className={step.at ? "font-mono text-muted" : "text-subtle"}>
                  {formatDateTime(step.at)}
                </span>
              </li>
            ))}
          </ol>
        </section>
      </div>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-muted">商品明细</h2>
        <div className="overflow-hidden rounded-lg border border-line">
          <table className="w-full text-left text-sm">
            <thead className="bg-surface text-xs uppercase tracking-wide text-subtle">
              <tr>
                <th className="px-4 py-2 font-medium">SKU</th>
                <th className="px-4 py-2 font-medium">商品</th>
                <th className="px-4 py-2 text-right font-medium">单价</th>
                <th className="px-4 py-2 text-right font-medium">数量</th>
                <th className="px-4 py-2 text-right font-medium">小计</th>
              </tr>
            </thead>
            <tbody>
              {order.items.map((item) => (
                <tr key={item.order_item_id} className="border-t border-line">
                  <td className="px-4 py-2 font-mono text-xs text-muted">{item.sku}</td>
                  <td className="px-4 py-2 text-muted">{item.name}</td>
                  <td className="px-4 py-2 text-right font-mono text-muted">
                    {formatCents(item.unit_price_cents)}
                  </td>
                  <td className="px-4 py-2 text-right font-mono text-muted">
                    {item.quantity}
                  </td>
                  <td className="px-4 py-2 text-right font-mono text-ink">
                    {formatCents(item.amount_cents)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
