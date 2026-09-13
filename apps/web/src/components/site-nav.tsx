"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const links = [
  { href: "/", label: "总览" },
  { href: "/orders", label: "业务后台" },
  { href: "/console", label: "Agent 操作台" },
  { href: "/approvals", label: "审批审计中心" },
  { href: "/governance", label: "执行与审计" },
];

export function SiteNav() {
  const pathname = usePathname();

  return (
    <header className="border-b border-neutral-800 bg-neutral-950/80 backdrop-blur">
      <div className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-3">
        <span className="font-mono text-sm font-semibold tracking-tight text-emerald-400">
          GuardRail
        </span>
        <nav className="flex flex-wrap gap-1 text-sm">
          {links.map((link) => {
            const active = pathname === link.href;
            return (
              <Link
                key={link.href}
                href={link.href}
                className={
                  active
                    ? "rounded-md bg-neutral-800 px-3 py-1.5 text-neutral-100"
                    : "rounded-md px-3 py-1.5 text-neutral-400 hover:bg-neutral-900 hover:text-neutral-200"
                }
              >
                {link.label}
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
