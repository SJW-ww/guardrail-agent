import type { AuditEntry, RunView } from "@guardrail/contracts";
import Link from "next/link";

import { ApiErrorNotice } from "@/components/api-error-notice";
import { RunBadge } from "@/components/status-badge";
import { listAudit, listRuns } from "@/lib/api";
import { diffSnapshots, describeValue } from "@/lib/diff";
import { formatDateTime } from "@/lib/format";

/**
 * 执行与审计看板。
 *
 * 这一页回答的是「Agent 到底改了什么」:
 * 上面是每次执行的推进进度与断点,下面是每一次写操作的 before → after。
 * 数据全部来自 `agent_run` / `agent_step` / `audit_log`,前端不持有任何本地状态。
 */
export default async function GovernancePage() {
  let runs: RunView[] | null = null;
  let audit: AuditEntry[] | null = null;
  let error: unknown = null;

  try {
    const [runPage, auditPage] = await Promise.all([
      listRuns({ limit: 10 }),
      listAudit({ limit: 20 }),
    ]);
    runs = runPage.items;
    audit = auditPage.items;
  } catch (cause) {
    error = cause;
  }

  if (!runs || !audit) {
    return (
      <div className="space-y-6">
        <h1 className="text-xl font-semibold">执行与审计</h1>
        <ApiErrorNotice error={error} />
      </div>
    );
  }

  return (
    <div className="space-y-8">
      <section className="space-y-2">
        <h1 className="text-xl font-semibold">执行与审计</h1>
        <p className="max-w-3xl text-sm leading-relaxed text-neutral-400">
          每次写入都走同一条治理链路:抢幂等账本 → 业务写 → 审计写 → 落 checkpoint,
          <span className="text-neutral-200">四件事同一次提交</span>
          。所以「步骤成功」永远不会是假的,进程被 kill -9 之后也能从断点接着跑。
        </p>
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-neutral-300">最近的执行</h2>
        <div className="overflow-hidden rounded-lg border border-neutral-800">
          <table className="w-full text-left text-sm">
            <thead className="bg-neutral-900/60 text-xs uppercase tracking-wide text-neutral-500">
              <tr>
                <th className="px-4 py-2 font-medium">执行</th>
                <th className="px-4 py-2 font-medium">意图</th>
                <th className="px-4 py-2 font-medium">状态</th>
                <th className="px-4 py-2 font-medium">断点</th>
                <th className="px-4 py-2 font-medium">尝试</th>
                <th className="px-4 py-2 font-medium">创建时间</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.run_uid} className="border-t border-neutral-800/70">
                  <td className="px-4 py-2">
                    <Link
                      href={`/governance/${run.run_uid}`}
                      className="font-mono text-emerald-400 hover:underline"
                    >
                      {run.run_uid.slice(0, 8)}
                    </Link>
                  </td>
                  <td className="max-w-xs truncate px-4 py-2 text-neutral-300">{run.goal}</td>
                  <td className="px-4 py-2">
                    <RunBadge status={run.status} />
                  </td>
                  <td className="px-4 py-2 font-mono text-xs text-neutral-400">
                    {run.checkpoint_seq} / {run.plan.length}
                    {run.waiting_ref ? ` · ${run.waiting_ref}` : ""}
                  </td>
                  <td className="px-4 py-2 font-mono text-xs text-neutral-500">{run.attempt}</td>
                  <td className="px-4 py-2 font-mono text-xs text-neutral-500">
                    {formatDateTime(run.created_at)}
                  </td>
                </tr>
              ))}
              {runs.length === 0 && (
                <tr className="border-t border-neutral-800/70">
                  <td colSpan={6} className="px-4 py-6 text-center text-neutral-500">
                    还没有执行记录,去「Agent 操作台」生成一条提议试试
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-neutral-300">写操作审计(最近 20 条)</h2>
        <p className="text-xs text-neutral-500">
          只读工具不写审计;幂等重放也不写 —— 审计行代表「世界上真的发生过一次写」。
        </p>
        <div className="space-y-3">
          {audit.map((entry) => (
            <AuditCard key={entry.id} entry={entry} />
          ))}
          {audit.length === 0 && (
            <p className="rounded-lg border border-neutral-800 px-4 py-6 text-center text-sm text-neutral-500">
              还没有写操作
            </p>
          )}
        </div>
      </section>
    </div>
  );
}

function AuditCard({ entry }: { entry: AuditEntry }) {
  const changes = diffSnapshots(entry.before ?? {}, entry.after ?? {});

  return (
    <article className="space-y-3 rounded-lg border border-neutral-800 bg-neutral-900/40 p-4">
      <header className="flex flex-wrap items-center gap-3 text-xs">
        <span className="font-mono text-neutral-200">{entry.tool_name}</span>
        <span
          className={
            entry.outcome === "SUCCEEDED"
              ? "rounded bg-emerald-500/10 px-2 py-0.5 text-emerald-300"
              : "rounded bg-rose-500/10 px-2 py-0.5 text-rose-300"
          }
        >
          {entry.outcome === "SUCCEEDED" ? "成功" : "失败"}
        </span>
        <span className="text-neutral-500">actor={entry.actor}</span>
        <span className="text-neutral-500">
          run={(entry.run_uid ?? "-").slice(0, 8)} step={entry.step_seq ?? "-"}
        </span>
        <span className="font-mono text-neutral-600">trace={entry.trace_id.slice(0, 8)}</span>
        <span className="ml-auto font-mono text-neutral-500">
          {formatDateTime(entry.created_at)}
        </span>
      </header>

      {entry.reason && <p className="text-sm text-neutral-400">理由:{entry.reason}</p>}

      {changes.length > 0 ? (
        <ul className="space-y-1 font-mono text-xs">
          {changes.map((change) => (
            <li key={change.path} className="flex flex-wrap items-baseline gap-2">
              <span className="text-neutral-400">{change.path}</span>
              <span className="text-rose-300/80 line-through">{describeValue(change.before)}</span>
              <span className="text-neutral-600">→</span>
              <span className="text-emerald-300">{describeValue(change.after)}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="font-mono text-xs text-neutral-500">(失败记录:没有产生前后值变化)</p>
      )}

      <details className="text-xs text-neutral-500">
        <summary className="cursor-pointer select-none">查看完整快照</summary>
        <div className="mt-2 grid gap-2 md:grid-cols-2">
          <pre className="max-h-64 overflow-auto rounded-md bg-neutral-950 p-3 font-mono text-[11px] text-neutral-400">
            {JSON.stringify(entry.before, null, 2)}
          </pre>
          <pre className="max-h-64 overflow-auto rounded-md bg-neutral-950 p-3 font-mono text-[11px] text-neutral-400">
            {JSON.stringify(entry.after, null, 2)}
          </pre>
        </div>
      </details>
    </article>
  );
}
