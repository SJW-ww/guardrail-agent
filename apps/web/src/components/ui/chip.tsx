import type { ReactNode } from "react";

type Tone = "neutral" | "info" | "success" | "warn" | "danger" | "brand";

const TONE: Record<Tone, string> = {
  neutral: "border-line-strong bg-raised text-muted",
  info: "border-sky-500/30 bg-sky-500/10 text-sky-300",
  success: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
  warn: "border-amber-500/30 bg-amber-500/10 text-amber-300",
  danger: "border-rose-500/30 bg-rose-500/10 text-rose-300",
  brand: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
};

/** 小标签。信息密度高的看板里,用它承载"状态/等级/口径"这类短事实。 */
export function Chip({
  children,
  tone = "neutral",
  title,
  mono = false,
}: {
  children: ReactNode;
  tone?: Tone;
  title?: string;
  mono?: boolean;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded-md border px-2 py-0.5 text-xs leading-5 ${
        mono ? "font-mono" : ""
      } ${TONE[tone]}`}
    >
      {children}
    </span>
  );
}
