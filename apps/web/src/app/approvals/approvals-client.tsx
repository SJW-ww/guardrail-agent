"use client";

import {
  REASON_CODE_LABELS,
  type TicketStatus,
  type TicketSummary,
} from "@guardrail/contracts";
import { useState } from "react";

import { ApiErrorNotice } from "@/components/api-error-notice";
import { TicketBadge } from "@/components/status-badge";
import { approveTicket, closeTicket, listTickets, refundTicket, rejectTicket } from "@/lib/api";
import { formatCents, formatDateTime } from "@/lib/format";

type Scope = "PENDING" | "ALL";

const OPERATOR = "supervisor-01";
const FINANCE = "finance-01";

export function ApprovalsClient({ initialTickets }: { initialTickets: TicketSummary[] }) {
  const [tickets, setTickets] = useState(initialTickets);
  const [scope, setScope] = useState<Scope>("PENDING");
  const [busyId, setBusyId] = useState<number | null>(null);
  const [rejectingId, setRejectingId] = useState<number | null>(null);
  const [rejectReason, setRejectReason] = useState("");
  const [error, setError] = useState<unknown>(null);

  async function reload(next: Scope) {
    setScope(next);
    setError(null);
    try {
      const data = await listTickets(next === "ALL" ? { limit: 50 } : { status: "PENDING", limit: 50 });
      setTickets(data.items);
    } catch (cause) {
      setError(cause);
    }
  }

  async function act(ticketId: number, task: () => Promise<TicketSummary>) {
    setBusyId(ticketId);
    setError(null);
    try {
      const updated = await task();
      // 就地更新,让「批准 → 退款」这一步不用来回切筛选
      setTickets((prev) =>
        prev.map((ticket) => (ticket.ticket_id === updated.ticket_id ? updated : ticket)),
      );
      setRejectingId(null);
      setRejectReason("");
    } catch (cause) {
      setError(cause);
    } finally {
      setBusyId(null);
    }
  }

  const pendingCount = tickets.filter((ticket) => ticket.status === "PENDING").length;

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <div className="flex gap-1">
          {(["PENDING", "ALL"] as Scope[]).map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => reload(item)}
              className={`rounded-md px-3 py-1.5 ${
                scope === item
                  ? "bg-neutral-800 text-neutral-100"
                  : "text-neutral-400 hover:bg-neutral-900"
              }`}
            >
              {item === "PENDING" ? "待审批" : "全部"}
            </button>
          ))}
        </div>
        <span className="font-mono text-xs text-neutral-500">
          当前列表 {tickets.length} 条 · 待审批 {pendingCount} 条
        </span>
      </div>

      {error !== null && <ApiErrorNotice error={error} />}

      {tickets.length === 0 ? (
        <p className="rounded-lg border border-dashed border-neutral-800 p-6 text-sm text-neutral-500">
          没有待处理的工单。去 <span className="text-emerald-400">Agent 操作台</span>{" "}
          发起一条退款申请试试。
        </p>
      ) : (
        <div className="overflow-hidden rounded-lg border border-neutral-800">
          <table className="w-full text-left text-sm">
            <thead className="bg-neutral-900/60 text-xs uppercase tracking-wide text-neutral-500">
              <tr>
                <th className="px-4 py-2 font-medium">工单号</th>
                <th className="px-4 py-2 font-medium">订单</th>
                <th className="px-4 py-2 font-medium">原因</th>
                <th className="px-4 py-2 text-right font-medium">金额</th>
                <th className="px-4 py-2 font-medium">状态</th>
                <th className="px-4 py-2 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {tickets.map((ticket) => (
                <tr key={ticket.ticket_id} className="border-t border-neutral-800/70 align-top">
                  <td className="px-4 py-2">
                    <div className="font-mono text-xs text-neutral-300">{ticket.ticket_no}</div>
                    <div className="font-mono text-xs text-neutral-600">
                      {formatDateTime(ticket.requested_at)}
                    </div>
                  </td>
                  <td className="px-4 py-2 font-mono text-xs text-emerald-400">
                    {ticket.order_no}
                  </td>
                  <td className="px-4 py-2 text-neutral-400">
                    {REASON_CODE_LABELS[ticket.reason_code] ?? ticket.reason_code}
                  </td>
                  <td className="px-4 py-2 text-right font-mono text-neutral-200">
                    {formatCents(ticket.refund_amount_cents)}
                  </td>
                  <td className="px-4 py-2">
                    <TicketBadge status={ticket.status} />
                    {ticket.handled_by && (
                      <div className="mt-1 font-mono text-xs text-neutral-600">
                        {ticket.handled_by}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-2">
                    <RowActions
                      ticket={ticket}
                      busy={busyId === ticket.ticket_id}
                      rejecting={rejectingId === ticket.ticket_id}
                      rejectReason={rejectReason}
                      onRejectReason={setRejectReason}
                      onStartReject={() => {
                        setRejectingId(ticket.ticket_id);
                        setRejectReason("");
                      }}
                      onCancelReject={() => setRejectingId(null)}
                      onAct={(task) => act(ticket.ticket_id, task)}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

type RowActionsProps = {
  ticket: TicketSummary;
  busy: boolean;
  rejecting: boolean;
  rejectReason: string;
  onRejectReason: (value: string) => void;
  onStartReject: () => void;
  onCancelReject: () => void;
  onAct: (task: () => Promise<TicketSummary>) => void;
};

function RowActions({
  ticket,
  busy,
  rejecting,
  rejectReason,
  onRejectReason,
  onStartReject,
  onCancelReject,
  onAct,
}: RowActionsProps) {
  const status: TicketStatus = ticket.status;

  if (status === "PENDING") {
    return (
      <div className="space-y-2">
        <div className="flex flex-wrap gap-1">
          <button
            type="button"
            disabled={busy}
            onClick={() => onAct(() => approveTicket(ticket.ticket_id, OPERATOR))}
            className="rounded bg-emerald-700 px-2 py-1 text-xs text-white hover:bg-emerald-600 disabled:opacity-50"
          >
            批准
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onStartReject}
            className="rounded border border-rose-900 px-2 py-1 text-xs text-rose-300 hover:bg-rose-950/40 disabled:opacity-50"
          >
            拒绝
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => onAct(() => closeTicket(ticket.ticket_id, OPERATOR))}
            className="rounded px-2 py-1 text-xs text-neutral-500 hover:text-neutral-300 disabled:opacity-50"
          >
            关闭
          </button>
        </div>
        {rejecting && (
          <div className="flex gap-1">
            <input
              value={rejectReason}
              onChange={(event) => onRejectReason(event.target.value)}
              placeholder="拒绝理由"
              className="w-40 rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-xs outline-none focus:border-rose-700"
            />
            <button
              type="button"
              disabled={busy || rejectReason.trim().length === 0}
              onClick={() =>
                onAct(() =>
                  rejectTicket(ticket.ticket_id, { reason: rejectReason }, OPERATOR),
                )
              }
              className="rounded bg-rose-700 px-2 py-1 text-xs text-white disabled:opacity-50"
            >
              提交
            </button>
            <button
              type="button"
              onClick={onCancelReject}
              className="rounded px-2 py-1 text-xs text-neutral-500"
            >
              取消
            </button>
          </div>
        )}
      </div>
    );
  }

  if (status === "APPROVED") {
    return (
      <button
        type="button"
        disabled={busy}
        onClick={() => onAct(() => refundTicket(ticket.ticket_id, FINANCE))}
        className="rounded bg-sky-700 px-2 py-1 text-xs text-white hover:bg-sky-600 disabled:opacity-50"
      >
        执行退款
      </button>
    );
  }

  return <span className="text-xs text-neutral-600">—</span>;
}

