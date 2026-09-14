import type { ReactNode } from "react";

/** 一块"被拿起来的"内容。层级靠 surface / line 区分,不靠边框粗细堆叠。 */
export function Card({
  children,
  className = "",
  tone = "default",
}: {
  children: ReactNode;
  className?: string;
  tone?: "default" | "warn" | "danger" | "success";
}) {
  const toneClass = {
    default: "border-line bg-surface",
    warn: "border-amber-500/30 bg-amber-500/[0.04]",
    danger: "border-rose-500/30 bg-rose-500/[0.04]",
    success: "border-emerald-500/30 bg-emerald-500/[0.04]",
  }[tone];

  return <section className={`rounded-xl border ${toneClass} ${className}`}>{children}</section>;
}

export function CardHeader({
  title,
  hint,
  right,
}: {
  title: ReactNode;
  hint?: ReactNode;
  right?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b border-line px-4 py-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="text-sm font-semibold text-ink">{title}</h3>
        {hint ? <span className="text-xs text-subtle">{hint}</span> : null}
      </div>
      {right}
    </header>
  );
}

export function CardBody({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`px-4 py-3 ${className}`}>{children}</div>;
}
