"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { IdentityPanel } from "@/components/layout/identity-panel";

type NavItem = { href: string; label: string; hint: string };

/**
 * 侧边栏的分组就是这套系统的心智模型:
 * 业务系统(被治理的对象)· Agent(提议的入口)· 治理(裁决、审批、审计)。
 * 导航顺序照着治理链路的顺序排,而不是照着开发顺序排。
 */
const GROUPS: { group: string; items: NavItem[] }[] = [
  {
    group: "业务系统",
    items: [{ href: "/orders", label: "业务后台", hint: "订单与售后工单" }],
  },
  {
    group: "Agent",
    items: [{ href: "/console", label: "Agent 操作台", hint: "一句人话 → 一条提议" }],
  },
  {
    group: "治理",
    items: [
      { href: "/approvals", label: "审批审计中心", hint: "谁签的字,签够没有" },
      { href: "/governance", label: "执行与审计", hint: "改了什么,能不能续跑" },
    ],
  },
];

function isActive(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function AppShell({ children, footer }: { children: ReactNode; footer?: ReactNode }) {
  const pathname = usePathname();

  // 登录页不该有侧边栏:还没证明你是谁,先别给你一张地图
  if (pathname === "/login") {
    return <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-8">{children}</main>;
  }

  return (
    <div className="flex min-h-screen">
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r border-line bg-surface/60 lg:flex">
        <Link href="/" className="block border-b border-line px-4 py-4">
          <span className="font-mono text-sm font-semibold tracking-tight text-brand">
            GuardRail
          </span>
          <span className="mt-1 block text-xs leading-relaxed text-subtle">
            Agent 写操作安全执行层
          </span>
        </Link>

        <nav className="flex-1 space-y-5 overflow-y-auto px-3 py-4">
          <div className="space-y-1">
            <Link
              href="/"
              className={`flex items-center rounded-lg px-3 py-2 text-sm transition-colors ${
                pathname === "/"
                  ? "bg-raised text-ink"
                  : "text-muted hover:bg-raised/60 hover:text-ink"
              }`}
            >
              总览
            </Link>
          </div>

          {GROUPS.map(({ group, items }) => (
            <div key={group} className="space-y-1">
              <p className="px-3 text-[11px] font-medium tracking-wider text-subtle uppercase">
                {group}
              </p>
              {items.map((item) => {
                const active = isActive(pathname, item.href);
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    aria-current={active ? "page" : undefined}
                    className={`block rounded-lg px-3 py-2 transition-colors ${
                      active ? "bg-raised text-ink" : "text-muted hover:bg-raised/60 hover:text-ink"
                    }`}
                  >
                    <span className="block text-sm">{item.label}</span>
                    <span className="mt-0.5 block text-[11px] text-subtle">{item.hint}</span>
                  </Link>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="border-t border-line p-2">
          {footer}
          <IdentityPanel />
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        {/* 窄屏下侧边栏收起,导航横向铺开 —— 手机上也要能走完同一条链路 */}
        <header className="sticky top-0 z-10 border-b border-line bg-canvas/90 backdrop-blur lg:hidden">
          <div className="flex items-center gap-3 px-4 py-3">
            <Link href="/" className="font-mono text-sm font-semibold text-brand">
              GuardRail
            </Link>
          </div>
          <nav className="flex gap-1 overflow-x-auto px-3 pb-2 text-sm">
            {[{ href: "/", label: "总览" }, ...GROUPS.flatMap((g) => g.items)].map((item) => {
              const active = isActive(pathname, item.href) || pathname === item.href;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`shrink-0 rounded-md px-3 py-1.5 ${
                    active ? "bg-raised text-ink" : "text-muted hover:text-ink"
                  }`}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>
        </header>

        <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-8">{children}</main>
      </div>
    </div>
  );
}
