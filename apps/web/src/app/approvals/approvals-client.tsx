"use client";

import {
  REASON_CODE_LABELS,
  type TicketStatus,
  type TicketSummary,
} from "@guardrail/contracts";
import { useCallback, useEffect, useState } from "react";

import { ApiErrorNotice } from "@/components/api-error-notice";
import { TicketBadge } from "@/components/status-badge";
import { EmptyState } from "@/components/ui/empty-state";
import { Table, TBody, Td, Th, THead, Tr } from "@/components/ui/table";
import { approveTicket, closeTicket, listTickets, refundTicket, rejectTicket } from "@/lib/api";
import { formatCents, formatDateTime } from "@/lib/format";
import { useIdentity } from "@/lib/identity";

type Scope = "PENDING" | "ALL";
type Sort = "recent" | "amount";

const PAGE_SIZE = 20;

// 未登录时(本地演示)用固定的演示身份,登录后一律以自己的名义签署。
// 这两个常量不该出现在生产路径上 —— 它们是「没登录也能试用」的兜底。
const DEMO_APPROVER = "supervisor-01";
const DEMO_CASHIER = "finance-01";

export function ApprovalsClient({
  initialTickets,
  initialTotal,
}: {
  initialTickets: TicketSummary[];
  initialTotal: number;
}) {
  const [tickets, setTickets] = useState(initialTickets);
  const [scope, setScope] = useState<Scope>("PENDING");
  const [sort, setSort] = useState<Sort>("recent");
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(initialTotal);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [rejectingId, setRejectingId] = useState<number | null>(null);
  const [rejectReason, setRejectReason] = useState("");
  const [error, setError] = useState<unknown>(null);
  const { principal } = useIdentity();
  const approver = principal?.username ?? DEMO_APPROVER;
  const cashier = principal?.username ?? DEMO_CASHIER;

  const load = useCallback(
    async (next: { scope: Scope; page: number }) => {
      setLoading(true);
      setError(null);
      try {
        const data = await listTickets({
          status: next.scope === "PENDING" ? "PENDING" : undefined,
          limit: PAGE_SIZE,
          offset: next.page * PAGE_SIZE,
        });
        setTickets(data.items);
        setTotal(data.total);
      } catch (cause) {
        setError(cause);
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  // 首次进入用的初始数据来自服务端,不用再打一次接口
  useEffect(() => {
    if (scope === "PENDING" && page === 0) return;
    void load({ scope, page });
  }, [scope, page, load]);

  async function act(ticketId: number, task: () => Promise<TicketSummary>) {
    setBusyId(ticketId);
    setError(null);
    try {
      const updated = await task();
      // 就地更新,让「批准 → 执行退款」这一步不用来回切筛选
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

  const sorted = [...tickets].sort((a, b) => {
    if (sort === "amount") {
      return (b.refund_amount_cents ?? 0) - (a.refund_amount_cents ?? 0);
    }
    return new Date(b.requested_at).getTime() - new Date(a.requested_at).getTime();
  });

  const pendingCount = sorted.filter((ticket) => ticket.status === "PENDING").length;
  const lastPage = Math.max(0, Math.ceil(total / PAGE_SIZE) - 1);

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
        <div className="flex gap-1">
          {(["PENDING", "ALL"] as Scope[]).map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => {
                setScope(item);
                setPage(0);
              }}
              className={`rounded-md px-3 py-1.5 ${
                scope === item
                  ? "bg-raised text-ink"
                  : "text-muted hover:bg-raised/60 hover:text-ink"
              }`}
            >
              {item === "PENDING" ? "待审批" : "全部"}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-1 text-xs">
          <span className="text-subtle">排序</span>
          {(
            [
              ["recent", "最新优先"],
              ["amount", "金额优先"],
            ] as [Sort, string][]
          ).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setSort(key)}
              className={`rounded-md border px-2 py-1 ${
                sort === key
                  ? "border-line-strong text-ink"
                  : "border-transparent text-subtle hover:text-muted"
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        <span className="font-mono text-xs text-subtle">
          共 {total} 条 · 本页 {sorted.length} 条 · 待审批 {pendingCount} 条
          {loading ? " · 加载中…" : ""}
        </span>

        <span className="text-xs text-subtle">
          {principal
            ? `以 ${principal.actor} 的名义签署`
            : "未登录:以本地演示身份签署(生产环境会被拒绝)"}
        </span>
      </div>

      {error !== null && <ApiErrorNotice error={error} />}

      {sorted.length === 0 ? (
        <EmptyState>
          没有待处理的工单。去 Agent 操作台发起一条退款申请试试。
        </EmptyState>
      ) : (
        <Table>
          <THead>
            <Th>工单号</Th>
            <Th>订单</Th>
            <Th>原因</Th>
            <Th className="text-right">金额</Th>
            <Th className="w-28">状态</Th>
            <Th className="w-52">操作</Th>
          </THead>
          <TBody>
            {sorted.map((ticket) => (
              <Tr key={ticket.ticket_id} className="align-top">
                <Td>
                  <div className="font-mono text-xs text-ink">{ticket.ticket_no}</div>
                  <div className="font-mono text-xs text-subtle">
                    {formatDateTime(ticket.requested_at)}
                  </div>
                </Td>
                <Td mono className="text-xs text-brand">
                  {ticket.order_no}
                </Td>
                <Td className="text-muted">
                  {REASON_CODE_LABELS[ticket.reason_code] ?? ticket.reason_code}
                </Td>
                <Td mono className="text-right text-ink">
                  {formatCents(ticket.refund_amount_cents)}
                </Td>
                <Td>
                  <TicketBadge status={ticket.status} />
                  {ticket.handled_by && (
                    <div className="mt-1 font-mono text-xs text-subtle">{ticket.handled_by}</div>
                  )}
                </Td>
                <Td>
                  <RowActions
                    ticket={ticket}
                    approver={approver}
                    cashier={cashier}
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
                </Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      )}

      {total > PAGE_SIZE && (
        <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-subtle">
          <span>
            第 {page * PAGE_SIZE + 1}–{page * PAGE_SIZE + sorted.length} 条 / 共 {total} 条
          </span>
          <span className="flex gap-2">
            <button
              type="button"
              disabled={page === 0 || loading}
              onClick={() => setPage((prev) => Math.max(0, prev - 1))}
              className="rounded-md border border-line px-3 py-1.5 text-muted hover:border-line-strong hover:text-ink disabled:opacity-40"
            >
              上一页
            </button>
            <button
              type="button"
              disabled={page >= lastPage || loading}
              onClick={() => setPage((prev) => prev + 1)}
              className="rounded-md border border-line px-3 py-1.5 text-muted hover:border-line-strong hover:text-ink disabled:opacity-40"
            >
              下一页
            </button>
          </span>
        </div>
      )}
    </section>
  );
}

type RowActionsProps = {
  ticket: TicketSummary;
  /** 签署人身份:登录了就用自己的,没登录才退回演示身份 */
  approver: string;
  cashier: string;
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
  approver,
  cashier,
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
            onClick={() => onAct(() => approveTicket(ticket.ticket_id, approver))}
            className="rounded-md bg-emerald-700 px-2.5 py-1 text-xs text-white hover:bg-emerald-600 disabled:opacity-50"
          >
            批准
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onStartReject}
            className="rounded-md border border-rose-900 px-2.5 py-1 text-xs text-rose-300 hover:bg-rose-950/40 disabled:opacity-50"
          >
            拒绝
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => onAct(() => closeTicket(ticket.ticket_id, approver))}
            className="rounded-md px-2.5 py-1 text-xs text-subtle hover:text-muted disabled:opacity-50"
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
              className="w-40 rounded-md border border-line-strong bg-canvas px-2 py-1 text-xs text-ink outline-none focus:border-rose-700"
            />
            <button
              type="button"
              disabled={busy || rejectReason.trim().length === 0}
              onClick={() =>
                onAct(() => rejectTicket(ticket.ticket_id, { reason: rejectReason }, approver))
              }
              className="rounded-md bg-rose-700 px-2.5 py-1 text-xs text-white disabled:opacity-50"
            >
              提交
            </button>
            <button
              type="button"
              onClick={onCancelReject}
              className="rounded-md px-2 py-1 text-xs text-subtle"
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
        onClick={() => onAct(() => refundTicket(ticket.ticket_id, cashier))}
        className="rounded-md bg-sky-700 px-2.5 py-1 text-xs text-white hover:bg-sky-600 disabled:opacity-50"
      >
        执行退款
      </button>
    );
  }

  return <span className="text-xs text-subtle">—</span>;
}
