import type { AuditEntry, AuditOutcome, RunView } from "@guardrail/contracts";
import Link from "next/link";

import { ApiErrorNotice } from "@/components/api-error-notice";
import { RunBadge } from "@/components/status-badge";
import { Chip } from "@/components/ui/chip";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";
import { Table, TBody, Td, Th, THead, Tr } from "@/components/ui/table";
import { listAudit, listRuns, listTools } from "@/lib/api";
import { diffSnapshots, describeValue } from "@/lib/diff";
import { formatDateTime } from "@/lib/format";

const PAGE_SIZE = 20;

type SearchParams = {
  outcome?: string;
  tool?: string;
  offset?: string;
};

function asOutcome(raw: string | undefined): AuditOutcome | undefined {
  return raw === "SUCCEEDED" || raw === "FAILED" ? raw : undefined;
}

function buildQuery(params: Record<string, string | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value) search.set(key, value);
  }
  const serialized = search.toString();
  return serialized ? `?${serialized}` : "";
}

/**
 * 执行与审计看板。
 *
 * 这一页回答的是「Agent 到底改了什么」:
 * 上面是每次执行的推进进度与断点,下面是每一次写操作的 before → after。
 * 数据全部来自 `agent_run` / `agent_step` / `audit_log`,前端不持有任何本地状态。
 */
export default async function GovernancePage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const params = await searchParams;
  const outcome = asOutcome(params.outcome);
  const tool = params.tool || undefined;
  const offset = Number.parseInt(params.offset ?? "0", 10) || 0;

  let runs: RunView[] | null = null;
  let audit: AuditEntry[] | null = null;
  let auditTotal = 0;
  let tools: string[] = [];
  let error: unknown = null;

  try {
    const [runPage, auditPage, toolList] = await Promise.all([
      listRuns({ limit: 10 }),
      listAudit({ limit: PAGE_SIZE, offset, outcome, tool_name: tool }),
      listTools(),
    ]);
    runs = runPage.items;
    audit = auditPage.items;
    auditTotal = auditPage.total;
    tools = toolList.map((item) => item.name);
  } catch (cause) {
    error = cause;
  }

  if (!runs || !audit) {
    return (
      <div className="space-y-6">
        <PageHeader title="执行与审计" />
        <ApiErrorNotice error={error} />
      </div>
    );
  }

  const filtered = Boolean(outcome || tool);
  const from = auditTotal === 0 ? 0 : offset + 1;
  const to = offset + audit.length;
  const prev = Math.max(0, offset - PAGE_SIZE);
  const next = offset + PAGE_SIZE;

  return (
    <div className="space-y-8">
      <PageHeader
        title="执行与审计"
        lead={
          <>
            每次写入都走同一条治理链路:抢幂等账本 → 业务写 → 审计写 → 落 checkpoint,
            <span className="text-ink">四件事同一次提交</span>
            。所以「步骤成功」永远不会是假的,进程被 kill -9 之后也能从断点接着跑。
          </>
        }
      />

      <section className="space-y-3">
        <h2 className="text-sm font-semibold tracking-wide text-subtle uppercase">最近的执行</h2>
        <Table>
          <THead>
            <Th>执行</Th>
            <Th>意图</Th>
            <Th className="w-24">状态</Th>
            <Th className="w-28">断点</Th>
            <Th className="w-16">尝试</Th>
            <Th className="w-36">创建时间</Th>
          </THead>
          <TBody>
            {runs.map((run) => (
              <Tr key={run.run_uid}>
                <Td mono>
                  <Link
                    href={`/governance/${run.run_uid}`}
                    className="text-brand hover:underline"
                  >
                    {run.run_uid.slice(0, 8)}
                  </Link>
                </Td>
                <Td className="max-w-xs truncate text-ink">{run.goal}</Td>
                <Td>
                  <RunBadge status={run.status} />
                </Td>
                <Td mono className="text-xs text-muted">
                  {run.checkpoint_seq} / {run.plan.length}
                  {run.waiting_ref ? ` · ${run.waiting_ref}` : ""}
                </Td>
                <Td mono className="text-xs text-subtle">
                  {run.attempt}
                </Td>
                <Td mono className="text-xs text-subtle">
                  {formatDateTime(run.created_at)}
                </Td>
              </Tr>
            ))}
            {runs.length === 0 && (
              <EmptyState colSpan={6}>
                还没有执行记录,去「Agent 操作台」生成一条提议试试
              </EmptyState>
            )}
          </TBody>
        </Table>
      </section>

      <section className="space-y-3">
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <h2 className="text-sm font-semibold tracking-wide text-subtle uppercase">
            写操作审计
          </h2>
          <p className="text-xs text-subtle">
            只读工具不写审计;幂等重放也不写 —— 审计行代表「世界上真的发生过一次写」
          </p>
        </div>

        {/* 筛选走 URL:可分享、可后退,刷新不丢状态 */}
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <FilterChip href="/governance" active={!filtered}>
            全部
          </FilterChip>
          <FilterChip
            href={`/governance${buildQuery({ outcome: "SUCCEEDED" })}`}
            active={outcome === "SUCCEEDED"}
          >
            成功
          </FilterChip>
          <FilterChip
            href={`/governance${buildQuery({ outcome: "FAILED" })}`}
            active={outcome === "FAILED"}
          >
            失败
          </FilterChip>
          <span className="mx-1 h-4 w-px bg-line" />
          <FilterChip
            href={`/governance${buildQuery({ tool })}`}
            active={!tool}
          >
            全部工具
          </FilterChip>
          {tools.map((name) => (
            <FilterChip
              key={name}
              href={`/governance${buildQuery({ outcome, tool: name })}`}
              active={tool === name}
              mono
            >
              {name}
            </FilterChip>
          ))}
        </div>

        <div className="space-y-3">
          {audit.map((entry) => (
            <AuditCard key={entry.id} entry={entry} />
          ))}
          {audit.length === 0 && (
            <EmptyState>
              {filtered ? "当前筛选条件下没有审计记录" : "还没有写操作"}
            </EmptyState>
          )}
        </div>

        {auditTotal > 0 && (
          <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-subtle">
            <span>
              第 {from}–{to} 条 / 共 {auditTotal} 条
            </span>
            <span className="flex items-center gap-2">
              {offset > 0 ? (
                <Link
                  href={`/governance${buildQuery({ outcome, tool, offset: String(prev) })}`}
                  className="rounded-md border border-line px-3 py-1.5 text-muted hover:border-line-strong hover:text-ink"
                >
                  上一页
                </Link>
              ) : null}
              {to < auditTotal ? (
                <Link
                  href={`/governance${buildQuery({ outcome, tool, offset: String(next) })}`}
                  className="rounded-md border border-line px-3 py-1.5 text-muted hover:border-line-strong hover:text-ink"
                >
                  下一页
                </Link>
              ) : null}
            </span>
          </div>
        )}
      </section>
    </div>
  );
}

function FilterChip({
  href,
  active,
  mono = false,
  children,
}: {
  href: string;
  active: boolean;
  mono?: boolean;
  children: React.ReactNode;
}) {
  return (
    <Link
      href={href}
      className={`rounded-md border px-2.5 py-1 transition-colors ${
        active
          ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
          : "border-line text-muted hover:border-line-strong hover:text-ink"
      } ${mono ? "font-mono" : ""}`}
    >
      {children}
    </Link>
  );
}

/**
 * 一条写操作的审计卡片。
 *
 * 顶部那行是**扫描层**:不展开就能知道「谁、对什么、改了几个字段」。
 * 中间是字段级 diff(比原始 JSON 好读,也更能说明"到底动了什么"),
 * 完整快照收在折叠里 —— 只有排查时才需要它。
 */
function AuditCard({ entry }: { entry: AuditEntry }) {
  const changes = diffSnapshots(entry.before ?? {}, entry.after ?? {});
  const succeeded = entry.outcome === "SUCCEEDED";

  return (
    <article className="space-y-3 rounded-xl border border-line bg-surface p-4">
      <header className="flex flex-wrap items-center gap-3 text-xs">
        <span className="font-mono text-ink">{entry.tool_name}</span>
        <Chip tone={succeeded ? "success" : "danger"}>{succeeded ? "成功" : "失败"}</Chip>
        {entry.policy_decision && (
          <Chip
            tone={entry.policy_decision === "ALLOW" ? "info" : "warn"}
            title={entry.policy_reason ?? undefined}
          >
            策略 {entry.policy_decision}
          </Chip>
        )}
        <span className="text-muted">actor={entry.actor}</span>
        {entry.run_uid ? (
          <Link
            href={`/governance/${entry.run_uid}`}
            className="font-mono text-subtle hover:text-brand hover:underline"
            title="跳到这条审计所属的执行"
          >
            run={entry.run_uid.slice(0, 8)} step={entry.step_seq ?? "-"}
          </Link>
        ) : (
          <span className="font-mono text-subtle">step={entry.step_seq ?? "-"}</span>
        )}
        <span className="font-mono text-subtle">trace={entry.trace_id.slice(0, 8)}</span>
        <span className="ml-auto font-mono text-subtle">{formatDateTime(entry.created_at)}</span>
      </header>

      {entry.reason && <p className="text-sm text-muted">理由:{entry.reason}</p>}

      {entry.policy_reason && (
        <p className="text-xs text-subtle">
          <span className="font-mono">凭什么允许它写:</span> {entry.policy_reason}
        </p>
      )}

      {changes.length > 0 ? (
        <ul className="space-y-1 font-mono text-xs">
          {changes.map((change) => (
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
      ) : (
        <p className="font-mono text-xs text-subtle">(失败记录:没有产生前后值变化)</p>
      )}

      <details className="text-xs text-subtle">
        <summary className="cursor-pointer select-none">查看完整快照</summary>
        <div className="mt-2 grid gap-2 md:grid-cols-2">
          <pre className="max-h-64 overflow-auto rounded-md border border-line bg-canvas p-3 font-mono text-[11px] text-muted">
            {JSON.stringify(entry.before, null, 2)}
          </pre>
          <pre className="max-h-64 overflow-auto rounded-md border border-line bg-canvas p-3 font-mono text-[11px] text-muted">
            {JSON.stringify(entry.after, null, 2)}
          </pre>
        </div>
      </details>
    </article>
  );
}
