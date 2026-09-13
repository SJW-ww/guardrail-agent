/**
 * 审计快照的前后值对比。
 *
 * 审计表里存的是两份完整快照,但给人看不应该丢两坨 JSON 让人自己找不同 ——
 * 这个函数把 before/after 拍平成「哪个字段从什么变成了什么」。
 * 只在浏览器/服务端做展示,不参与任何判定逻辑。
 */

export type DiffKind = "added" | "removed" | "changed";

export type DiffEntry = {
  path: string;
  before: unknown;
  after: unknown;
  kind: DiffKind;
};

type Json = unknown;

function isPlainObject(value: Json): value is Record<string, Json> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function format(value: Json): string {
  if (value === undefined) return "—";
  if (value === null) return "null";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function describeValue(value: Json): string {
  return format(value);
}

export function diffSnapshots(before: Json, after: Json, prefix = ""): DiffEntry[] {
  if (isPlainObject(before) && isPlainObject(after)) {
    const keys = [...new Set([...Object.keys(before), ...Object.keys(after)])].sort();
    return keys.flatMap((key) =>
      diffSnapshots(before[key], after[key], prefix ? `${prefix}.${key}` : key),
    );
  }

  if (Array.isArray(before) && Array.isArray(after)) {
    const length = Math.max(before.length, after.length);
    return Array.from({ length }).flatMap((_, index) =>
      diffSnapshots(before[index], after[index], `${prefix}[${index}]`),
    );
  }

  const beforeEmpty = before === undefined || before === null;
  const afterEmpty = after === undefined || after === null;

  if (beforeEmpty && afterEmpty) return [];
  // 单边为空时继续下钻:数组新增一个实体,应该看到 tickets[0].status —— →
  // 而不是把整个对象当成一坨 JSON 丢出来
  if (beforeEmpty) return collectLeaves(after, prefix, "added");
  if (afterEmpty) return collectLeaves(before, prefix, "removed");
  if (JSON.stringify(before) === JSON.stringify(after)) return [];
  return [{ path: prefix, before, after, kind: "changed" }];
}

function collectLeaves(value: Json, path: string, kind: "added" | "removed"): DiffEntry[] {
  if (Array.isArray(value)) {
    return value.flatMap((item, index) => collectLeaves(item, `${path}[${index}]`, kind));
  }
  if (isPlainObject(value)) {
    return Object.entries(value).flatMap(([key, child]) =>
      collectLeaves(child, path ? `${path}.${key}` : key, kind),
    );
  }
  return [
    {
      path,
      before: kind === "added" ? undefined : value,
      after: kind === "added" ? value : undefined,
      kind,
    },
  ];
}
