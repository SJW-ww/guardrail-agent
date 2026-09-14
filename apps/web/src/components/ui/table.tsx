import type { ReactNode, ThHTMLAttributes } from "react";

/**
 * 表格的统一外壳。
 *
 * 治理看板里表格承担的是"扫一遍就知道发生了什么",所以:
 * 表头压暗、行之间细线、数字与标识用等宽字体 —— 对齐比装饰重要。
 */
export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-hidden rounded-xl border border-line">
      <table className="w-full text-left text-sm">{children}</table>
    </div>
  );
}

export function THead({ children }: { children: ReactNode }) {
  return (
    <thead className="bg-raised text-xs tracking-wide text-subtle">
      <tr>{children}</tr>
    </thead>
  );
}

export function Th({ children, ...rest }: ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th className="px-4 py-2 font-medium" {...rest}>
      {children}
    </th>
  );
}

export function TBody({ children }: { children: ReactNode }) {
  return <tbody>{children}</tbody>;
}

export function Tr({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <tr className={`border-t border-line ${className}`}>{children}</tr>;
}

export function Td({
  children,
  className = "",
  mono = false,
  colSpan,
}: {
  children: ReactNode;
  className?: string;
  mono?: boolean;
  colSpan?: number;
}) {
  return (
    <td colSpan={colSpan} className={`px-4 py-2 ${mono ? "font-mono" : ""} ${className}`}>
      {children}
    </td>
  );
}
