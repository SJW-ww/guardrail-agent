import type { ReactNode } from "react";

type PlaceholderProps = {
  title: string;
  milestone: string;
  children: ReactNode;
};

export function Placeholder({ title, milestone, children }: PlaceholderProps) {
  return (
    <section className="rounded-lg border border-dashed border-neutral-800 bg-neutral-900/40 p-6">
      <div className="mb-3 flex items-center gap-3">
        <h2 className="text-lg font-semibold text-neutral-100">{title}</h2>
        <span className="rounded bg-amber-500/10 px-2 py-0.5 font-mono text-xs text-amber-400">
          {milestone}
        </span>
      </div>
      <div className="space-y-2 text-sm leading-relaxed text-neutral-400">{children}</div>
    </section>
  );
}

