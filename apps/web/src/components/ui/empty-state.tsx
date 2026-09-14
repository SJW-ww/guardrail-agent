import type { ReactNode } from "react";

export function EmptyState({
  children,
  colSpan,
}: {
  children: ReactNode;
  /** 放进 <table> 的 tbody 时用它跨列 */
  colSpan?: number;
}) {
  const content = (
    <p className="px-4 py-8 text-center text-sm text-subtle">{children}</p>
  );
  if (colSpan === undefined) {
    return <div className="rounded-xl border border-dashed border-line">{content}</div>;
  }
  return (
    <tr className="border-t border-line">
      <td colSpan={colSpan}>{content}</td>
    </tr>
  );
}
