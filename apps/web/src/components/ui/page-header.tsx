import type { ReactNode } from "react";

/** 每个页面的第一屏:标题 + 一句话说清这一页回答什么问题 + 右侧可放操作或指标。 */
export function PageHeader({
  title,
  lead,
  right,
}: {
  title: string;
  lead?: ReactNode;
  right?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
      <div className="max-w-3xl space-y-1.5">
        <h1 className="text-xl font-semibold tracking-tight text-ink">{title}</h1>
        {lead ? <p className="text-sm leading-relaxed text-muted">{lead}</p> : null}
      </div>
      {right ? <div className="flex flex-wrap items-center gap-2">{right}</div> : null}
    </header>
  );
}
